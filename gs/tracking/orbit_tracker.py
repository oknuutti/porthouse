"""
    Orbit tracker
"""

import json
import asyncio
import datetime
from enum import IntEnum
from typing import Optional, NoReturn, List, Union

import aiormq.abc
import skyfield

from porthouse.core.basemodule_async import BaseModule, RPCError, rpc, queue, bind

from .utils import Satellite, CelestialObject, Pass, SkyfieldModuleMixin

ts = skyfield.api.load.timescale()


class OrbitTracker(SkyfieldModuleMixin, BaseModule):
    """ Module class to implement OrbitTracker. """

    TRACKER_TYPE = 'orbit'
    DEFAULT_PREAOS_TIME = 120

    def __init__(self, **kwargs):
        """
        Initialize module.
        """
        super().__init__(**kwargs)

        self.scheduler_enabled = kwargs.get("scheduler_enabled", True)

        # list of targets, associated rotators, and tracking status
        self.target_trackers = []

        # start tracking if we have a default target
        target_name = self.gs.config.get("default", None)
        rotators = self.gs.config.get("rotators", None)
        if target_name and rotators:
            loop = asyncio.get_event_loop()
            task = loop.create_task(self.add_target(target_name, rotators), name="orbit_tracker.add_target")
            task.add_done_callback(self.task_done_handler)

    @queue()
    @bind(exchange="scheduler", routing_key="#")
    async def scheduler_event(self, message: aiormq.abc.DeliveredMessage):
        """
        Listen to events generated by the scheduler
        """
        await asyncio.sleep(0)

        # Don't do anything automatic if scheduler not enabled
        if not self.scheduler_enabled:
            return

        try:
            event_body = json.loads(message.body)
        except ValueError as e:
            self.log.error('Failed to parse json: %s\n%s', e.args[0], message.body, exc_info=True)
            return

        if self.TRACKER_TYPE != event_body.get('tracker', ''):
            return

        routing_key = message.delivery['routing_key']
        self.log.debug(f"Scheduler event {routing_key} for target "
                       f"{event_body['target']} {event_body['rotators']} received")

        if routing_key == "task.start":
            kwargs = {k: v for k, v in event_body.items() if k in ["start_time", "end_time", "min_elevation",
                                                                   "min_max_elevation", "sun_max_elevation",
                                                                   "sunlit", "preaos_time"]}
            await self.add_target(event_body["target"], event_body["rotators"], **kwargs)

        elif routing_key == "task.end":
            await self.remove_target(event_body["target"], event_body["rotators"])

    @rpc()
    @bind("tracking", "orbit.rpc.#")
    async def rpc_handler(self, request_name: str, request_data: dict):
        """
        Handle RPC commands
        """
        request_name = request_name[6:]

        if request_name == "rpc.add_target":
            params = {k: v for k, v in request_data.items() if k in ["start_time", "end_time", "min_elevation",
                                                                     "min_max_elevation", "sun_max_elevation",
                                                                     "sunlit", "preaos_time", "high_accuracy"]}
            await self.add_target(request_data["target"], request_data["rotators"], **params)

        elif request_name == "rpc.remove_target":
            await self.remove_target(request_data["target"], request_data["rotators"])

        elif request_name == "rpc.status":
            await asyncio.sleep(0)
            return self._get_status_message()

        elif request_name == "rpc.get_target_position":
            target_name = request_data.get("target", "")

            if CelestialObject.is_class_of(target_name):
                target = await self.get_celestial_object(target_name)
            else:
                target = await self.get_satellite(target_name)

            if target is None:
                return None

            return target.to_dict()

        else:
            raise RPCError(f"No such command: {request_name}")

    async def add_target(self, target_name: str, rotators: List[str],
                         start_time: Union[None, str, datetime.datetime, skyfield.api.Time] = None,
                         end_time: Union[None, str, datetime.datetime, skyfield.api.Time] = None,
                         min_elevation: float = 0,
                         min_max_elevation: float = 0,
                         sun_max_elevation: float = None,
                         sunlit: bool = None,
                         preaos_time: int = DEFAULT_PREAOS_TIME,
                         high_accuracy: bool = None) -> NoReturn:
        """
        Set the tracking target for given rotators.
        """

        await asyncio.sleep(0)  # Make sure that something is awaited.

        if target_name is None or len(target_name) == 0:
            self.log.error("add_target: Target must not be None or empty", exc_info=True)
            return

        if rotators is None or len(rotators) == 0:
            self.log.error("add_target: Rotators must not be None or empty", exc_info=True)
            return

        if target_name in [tt.target.target_name for tt in self.target_trackers]:
            i = [tt.target.name for tt in self.target_trackers].index(target_name)
            self.log.warning(f"add_target: Target {target_name} is already tracked "
                             f"with {self.target_trackers[i].rotators}")
            return

        self.log.info(f"Starting to track target {target_name} with {rotators}")

        # NOTE: Other params except target_name not strictly needed for get_satellite or get_celestial_object
        #       as current pass is only of interest and scheduler takes care of min elevation etc filtering.
        #       However, AOS and LOS times could be different without them.
        if CelestialObject.is_class_of(target_name):
            target = await self.get_celestial_object(target_name, start_time=start_time, end_time=end_time,
                                                     min_elevation=min_elevation, min_max_elevation=min_max_elevation,
                                                     sun_max_elevation=sun_max_elevation, sunlit=sunlit,
                                                     partial_last_pass=True)
        else:
            target = await self.get_satellite(target_name, start_time=start_time, end_time=end_time,
                                              min_elevation=min_elevation, min_max_elevation=min_max_elevation,
                                              sun_max_elevation=sun_max_elevation, sunlit=sunlit)

        if target is None:
            self.log.error(f"add_target: Could not find target {target_name}")
            return

        next_pass = target.get_next_pass()
        if next_pass is None:
            self.log.error(f"add_target: No passes available for {target_name}")
            return

        await self.send_event("next_pass", target=target, rotators=rotators, **next_pass.to_dict())

        target_tracker = TargetTracker(self, target, rotators, preaos_time=preaos_time, high_accuracy=high_accuracy)
        self.target_trackers.append(target_tracker)
        await target_tracker.start()

    async def remove_target(self, target_name: str, rotators: List[str]) -> NoReturn:
        await asyncio.sleep(0)
        rotators = set(rotators)
        remove_idxs = []

        self.log.info(f"Stop tracking target {target_name} with {rotators}")

        for i, tt in enumerate(self.target_trackers):
            if tt.target.target_name == target_name and rotators.intersection(tt.rotators):
                await tt.stop(rotators)
                if len(tt.rotators) == 0:
                    remove_idxs.append(i)

        self.target_trackers = [tt for i, tt in enumerate(self.target_trackers) if i not in remove_idxs]

    async def broadcast_pointing(self, target: Union[Satellite, CelestialObject], rotators: List[str],
                                 az: float, el: float, range: float, range_rate: float, timestamp: float) -> None:
        """
        Broadcast pointing information
            - e.g. for the rotator, also for modem software doppler correction

        Args:
            target: Target name
            rotators: List of rotators that should track the target
            az: Target azimuth
            el: Target elevation
            range: Target distance
            range_rate: Target distance change rate
        """

        if el < 0:
            el = 0
        if az > 180:
            az -= 360

        await self.publish({
            "target": target.target_name,
            "rotators": rotators,
            "az": round(az, 2),
            "el": round(el, 2),
            "range": round(range, 2),
            "range_rate": round(range_rate, 2),
            "timestamp": timestamp,
        }, exchange="tracking", routing_key="target.position")

    async def send_event(self, event_name, target: Union[Satellite, CelestialObject], rotators: List[str], **params):
        """
        Send events (next_pass/preaos/aos/los), used e.g. by the rotator
        """

        self.log.info("%s event emitted for %s %s: %s", event_name, target.target_name, rotators, params)
        params.update({'target': target.target_name, 'rotators': rotators})
        await self.publish(params, exchange="event", routing_key=event_name)

    def _get_status_message(self):
        return [tt.get_status_message() for tt in self.target_trackers]


class TrackerStatus(IntEnum):
    """ Tracker states """
    DISABLED = 0
    WAITING = 1
    AOS = 2
    TRACKING = 3
    LOS = 4


class TargetTracker:
    def __init__(self, module: OrbitTracker, target: Union[Satellite, CelestialObject], rotators: List[str],
                 preaos_time=OrbitTracker.DEFAULT_PREAOS_TIME, status=TrackerStatus.WAITING, high_accuracy=None):
        self.module = module
        self.target = target
        self.rotators = rotators
        self.preaos_time = datetime.timedelta(seconds=preaos_time)
        self.status = status
        self.high_accuracy = isinstance(target, CelestialObject) if high_accuracy is None else high_accuracy
        self.asyncio_task = None

    async def setup(self) -> None:
        while self.target:
            await self.update_tracking()
            await asyncio.sleep(2)

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        self.asyncio_task = loop.create_task(self.setup(),
                                             name=f"TargetTracker-{self.target.target_name} {self.rotators}")
        self.asyncio_task.add_done_callback(self.module.task_done_handler)

    async def stop(self, rotators: List[str]) -> None:
        target = self.target
        stop_rotators = set(self.rotators).intersection(rotators)
        self.rotators = list(set(self.rotators).difference(rotators))
        if len(self.rotators) == 0:
            if self.asyncio_task is not None:
                self.asyncio_task.cancel()
            self.target = None

        if len(stop_rotators) > 0:
            await self.module.send_event("los", target=target, rotators=list(stop_rotators))
        else:
            # make sure something is awaited
            await asyncio.sleep(0)

    async def update_tracking(self) -> None:
        """
        Update tracking calculations
        """

        # make sure something is awaited
        await asyncio.sleep(0)

        if self.target is None:
            return

        # Update current prediction
        now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)

        next_pass = self.target.get_next_pass()
        if next_pass is None:
            self.module.log.critical(f"No passes for {self.target.target_name} {self.rotators}!")
            self.status = TrackerStatus.DISABLED

        # Tracking state machine:
        if self.status == TrackerStatus.WAITING:

            if self.module.debug:
                s = (next_pass.t_aos - now).total_seconds()
                m, s = divmod(s, 60)
                h, m = divmod(m, 60)

                if h > 0:
                    self.module.log.debug(f"AOS for {self.target.target_name} {self.rotators}"
                                          f" in {h:.0f} hour and {m:.0f} minutes")
                else:
                    self.module.log.debug(f"AOS for {self.target.target_name} {self.rotators}"
                                          f" in {m:.0f} minutes and {s:.0f} seconds")

            # Check if a pass is already going on
            if now >= next_pass.t_aos:
                await self.module.send_event("aos", target=self.target, rotators=self.rotators)
                self.status = TrackerStatus.TRACKING

            # Is AOS about to happen?
            elif now >= next_pass.t_aos - self.preaos_time:
                await self.module.send_event("preaos", target=self.target, rotators=self.rotators,
                                             **next_pass.to_dict())
                self.status = TrackerStatus.AOS

        elif self.status == TrackerStatus.AOS:

            # Did AOS happen?
            if now >= next_pass.t_aos:
                await self.module.send_event("aos", target=self.target, rotators=self.rotators)
                self.status = TrackerStatus.TRACKING

            elif self.module.debug:
                sec = (next_pass.t_aos - now).total_seconds()
                self.module.log.debug(f"AOS for {self.target.target_name} {self.rotators} in {sec:.0f} seconds")

        elif self.status == TrackerStatus.TRACKING:
            # Calculate the position 1 second in the future
            t = now + datetime.timedelta(seconds=1)
            pos = self.target.pos_at(t, accurate=self.high_accuracy)
            el, az, range, _, _, range_rate = pos.frame_latlon_and_rates(self.module.gs.pos)
            if self.high_accuracy:
                el, az, _ = pos.altaz('standard')  # include effect from atmospheric refraction

            if self.module.debug:
                m, s = divmod((next_pass.t_los - now).total_seconds(), 60)
                self.module.log.debug(f"LOS for {self.target.target_name} {self.rotators} in {m:.0f} minutes "
                                      f"{s:.0f} seconds, az={az.degrees:.1f} el={el.degrees:.1f} "
                                      f"rr={range_rate.m_per_s:.1f}")

            # Broadcast spacecraft position
            await self.module.broadcast_pointing(self.target, self.rotators, az=az.degrees, el=el.degrees,
                                                 range=range.m, range_rate=range_rate.m_per_s,
                                                 timestamp=t.timestamp())

            # Did LOS happen?
            if now >= next_pass.t_los:
                await self.module.send_event("los", target=self.target, rotators=self.rotators)
                self.status = TrackerStatus.LOS

        elif self.status == TrackerStatus.LOS:
            #
            # Handle LOS
            #
            self.module.log.debug(f"After LOS for {self.target.target_name} {self.rotators}")
            self.status = TrackerStatus.WAITING
            self.target.calculate_passes()

    def get_status_message(self):
        # Do we have target which has upcoming passes
        if self.target is not None and len(self.target.passes) > 0:

            if self.status == TrackerStatus.AOS:
                status = f"Pre-AOS for {self.target.target_name}"
                pass_info = self.target.get_next_pass()
            elif self.status == TrackerStatus.TRACKING:
                status = f"Tracking {self.target.target_name}"
                pass_info = self.target.get_next_pass()
            elif self.status == TrackerStatus.DISABLED:
                status = "Disabled"
                pass_info = None
            else:
                status = f"Waiting for {self.target.target_name}"
                pass_info = self.target.get_next_pass()

            if pass_info is not None:
                pass_info = pass_info.to_dict()

            status_message = {
                "target": self.target.target_name,
                "rotators": self.rotators,
                "status": status,
                "next_pass": pass_info,
            }

        else:
            status_message = {
                "target": None,
                "rotators": self.rotators,
                "status": None,
                "next_pass": None,
            }

        return status_message


if __name__ == "__main__":
    OrbitTracker(
        amqp_url="amqp://guest:guest@localhost:5672/",
        debug=True
    ).run()
