import os
import re
import argparse
import yaml
import math
from functools import lru_cache

from tqdm import tqdm
import numpy as np
import quaternion   # adds np.quaternion


# TODO:
#  - balance the data points so that they are evenly distributed in azimuth and elevation
#  ?- star detection to be based on Stetson detector so that motion blurred stars are not detected


def main():
    methods = ('leastsq', 'bfgs', 'nelder-mead')

    parser = argparse.ArgumentParser(description='Calibrate rotator based on measurements and ground truth values.')
    parser.add_argument('--input', type=str, help='Input file with measures and corresponding ground truth values. '
                                                  'Can be also a folder with FITS files.')
    parser.add_argument('--input-cache', type=str, help='If input is a folder of FITS files, save input in CSV format')
    parser.add_argument('--output', type=str, help='output file with fitted rotator parameters')
    parser.add_argument('--init', type=str, help='initial rotator model parameters')
    parser.add_argument('--plot', action='store_true', help='plot initial state and the resulting state')
    parser.add_argument('--fit', action='store_true', help='fit model to the data')
    parser.add_argument('--iters', type=int, default=2, help='how many outlier rejection iterations to run (default=2)')
    parser.add_argument('--rm-drift', type=int,
                        help='Use these many data points from start and end for linear encoder drift removal')
    parser.add_argument('--debug-model', action='store_true', help='debug model-to-real and real-to-model functions')
    parser.add_argument('--method', default=methods[0], choices=methods,
                        help=f'Optimization method, one of: {methods}, default: {methods[0]}')
    args = parser.parse_args()

    if args.plot:
        import matplotlib.pyplot as plt

    args.fit = args.fit or args.output

    rotator0 = AzElRotator.load(args.init) if args.init else AzElRotator()
    param_dict0 = rotator0.to_dict()
    params0 = [param_dict0[key] for key in ('el_off', 'az_off', 'el_gain', 'az_gain',
                                            'tilt_az', 'tilt_angle', 'lateral_tilt')]

    data = []
    ts = []
    if os.path.isfile(args.input) or args.input_cache and os.path.isfile(args.input_cache):
        input = args.input_cache if args.input_cache else args.input
        with open(input, 'r') as fh:
            for line in fh:
                if line.startswith('#') or line.isspace():
                    continue
                line, *_ = line.split('#')
                az, el, gt_az, gt_el = map(lambda x: float(x.strip()), line.split(',')[:4])
                extra = list(map(lambda x: x.strip(), line.split(',')[4:]))
                az, el = rotator0.to_motor(az, el)
                data.append([az, el, gt_az, gt_el])
                if len(extra) > 0:
                    ts.append(extra[0])
    else:
        assert os.path.isdir(args.input), 'Input must be a file or a directory with FITS files'
        from astropy.io import fits
        files = [f for f in os.listdir(args.input) if re.search(r"\.fits(\.(bz2|zip|gz))?$", f, re.IGNORECASE)]
        files = sorted(files, key=lambda x: [int(v) if v.isdigit() else v for v in re.split(r'_|\.|-', x)])
        for filename in tqdm(files, desc='Loading data'):
            with fits.open(os.path.join(args.input, filename)) as hdul:
                meta = dict(hdul[0].header)  # e.g. DATE-OBS, AZ-MNT, EL-MNT, AZ-SOLV, EL-SOLV
            if 'AZ-MNTDC' in meta and 'EL-MNTDC' in meta and np.any(np.abs([meta['AZ-MNTDC'], meta['EL-MNTDC']]) > 50):
                # skip data points where the rotator is moving too fast
                continue
            if 'AZ-MOUNT' in meta:  # TODO: remove this once the header is fixed
                meta['AZ-MNT'], meta['EL-MNT'] = meta['AZ-MOUNT'], meta['EL-MOUNT']
            if len({'AZ-SOLV', 'EL-SOLV', 'AZ-MNT', 'EL-MNT'}.intersection(meta.keys())) == 4:
                az, el = rotator0.to_motor(meta['AZ-MNT'], meta['EL-MNT'])
                data.append([az, el, meta['AZ-SOLV'], meta['EL-SOLV']])
                ts.append(meta['DATE-OBS'])
        if args.input_cache:
            with open(args.input_cache, 'w') as fh:
                fh.write('# az, el, gt_az, gt_el, ts\n')
                for (az, el, gt_az, gt_el), ts in zip(data, ts):
                    fh.write(f'{az}, {el}, {gt_az}, {gt_el}, {ts}\n')

    data = np.array(data)
    if len(ts) == len(data):
        ts = np.array(ts, dtype=np.datetime64)
    else:
        ts = None

    if len(data) == 0:
        print('Failed to load any data, exiting')
        return

    print(f'Loaded {len(data)} data points')
    data[:, 2] = wrapdeg(data[:, 2])
    data[np.logical_and(data[:, 0] > 180, np.abs(data[:, 0]-data[:, 2]) > 180), 2] += 360

    if args.rm_drift:
        # assume drift is proportional to distance slewed, remove it linearly
        def get_drift(rot, dat):
            n = args.rm_drift
            m_gt = np.array([rot.to_motor(az, el) for az, el in dat[:, 2:4]])
            err0 = np.mean(wrapdeg(m_gt[:n, :] - dat[:n, 0:2]), axis=0)
            err1 = np.mean(wrapdeg(m_gt[-n:, :] - dat[-n:, 0:2]), axis=0)
            distance = np.cumsum(np.abs(wrapdeg(np.diff(dat[:, 0:2], axis=0))), axis=0)
            err = err0 + (err1 - err0) * (distance / distance[-1, :])
            return - np.concatenate((err0[None, :], err), axis=0)
    else:
        get_drift = lambda rot, dat: np.zeros((len(dat), 2))

    def errorfn(params, dat) -> np.ndarray:
        rotator = AzElRotator.from_dict(dict(zip(('el_off', 'az_off', 'el_gain', 'az_gain',
                                                  'tilt_az', 'tilt_angle', 'lateral_tilt'), params)))
        drift = get_drift(rotator, dat)
        err = dat[:, 2:] - np.array([rotator.to_real(az, el, wrap=True) for az, el in dat[:, :2] - drift])
        err[:, 0] = wrapdeg(err[:, 0])
        return err

    def errnorm(params, dat) -> float:
        return np.linalg.norm(errorfn(params, dat), axis=1)

    def lossfn(params, dat) -> float:
        err = errorfn(params, dat)
        err = err.flatten()
        return err if args.method == 'leastsq' else np.mean(err ** 2)

    loss = lossfn(params0, data)
    if args.method == 'leastsq':
        loss = np.mean(loss ** 2)

    print('Initial state (loss=%.6f): %s' % (loss, param_dict0))

    # calibrate
    if args.fit:
        _data, _ts = data, ts
        for i in range(args.iters):
            if args.method == 'leastsq':
                from scipy.optimize import least_squares
                res = least_squares(lambda x: lossfn(x, _data), params0)
                param_dict = dict(zip(('el_off', 'az_off', 'el_gain', 'az_gain',
                                       'tilt_az', 'tilt_angle', 'lateral_tilt'), res.x))
                loss = np.mean(res.fun ** 2)
            else:
                from scipy.optimize import minimize
                res = minimize(lambda x: lossfn(x, _data), np.array(params0), method='BFGS' if args.method == 'bfgs' else 'Nelder-Mead')
                param_dict = dict(zip(('el_off', 'az_off', 'el_gain', 'az_gain',
                                       'tilt_az', 'tilt_angle', 'lateral_tilt'), res.x))
                loss = res.fun

            err = errnorm(res.x, _data)
            I = err < 3.0 * np.median(err)
            _data = _data[I, :]
            if _ts is not None:
                _ts = _ts[I]
            print('Fitted state i=%d n=%d loss=%.6f: %s' % (i, len(err), loss, param_dict))

        rotator = AzElRotator.from_dict(param_dict)

        if args.output:
            rotator.save(args.output)

    if args.debug_model:
        for az, el in data[:, :2]:
            az1, el1 = rotator.to_real(az, el, wrap=True)
            az2, el2 = rotator.to_motor(az1, el1, wrap=True)
            az2 += 360 if abs(az2-az) > 180 else 0
            print(f'az={az:.3f}, el={el:.3f} -> az1={az1:.3f}, el1={el1:.3f} -> az2={az2:.3f}, el2={el2:.3f}: '
                  f'err=[{az-az2:.4f}, {el-el2:.4f}]')

    # plot
    if args.plot:
        if args.init:
            drift = get_drift(rotator0, data)
            az, el = np.array([rotator0.to_real(az, el) for az, el in data[:, :2] - drift]).T
            plt.plot(az, el, '-+', label='initial')
        else:
            plt.plot(data[:, 0], data[:, 1], '-+', label='measured')
        plt.plot(data[:, 2], data[:, 3], '-o', label='ground truth', markerfacecolor='none')

        drift = get_drift(rotator, data)
        fitted = np.array([rotator.to_real(az, el, wrap=True) for az, el in data[:, :2] - drift])
        plt.plot(wrapdeg(fitted[:, 0]), fitted[:, 1], '-x', label='fitted')

        plt.xlabel('azimuth')
        plt.ylabel('elevation')
        plt.legend()

        if 1:
            err = errorfn(res.x, _data)
            fig, axs = plt.subplots(1, 2)
            for i, lbl in enumerate(('az', 'el')):
                axs[i].scatter(_data[:, 0], _data[:, 1], c=err[:, i], vmin=-0.5, vmax=0.5)
                axs[i].set_xlabel('azimuth')
                axs[i].set_ylabel('elevation')
                axs[i].set_title(f'{lbl} err [deg]')
            fig.tight_layout()

        if ts is not None:
            plt.figure()
            plt.plot(ts, errnorm(res.x, data), '.')
            plt.plot(_ts, errnorm(res.x, _data), '.')
            plt.xlabel('time')
            plt.ylabel('error [deg]')

        plt.show()


class AzElRotator:
    def __init__(self, el_off=0, az_off=0, el_gain=1, az_gain=1, tilt_az=0, tilt_angle=0, lateral_tilt=0):
        self.el_off = el_off
        self.az_off = az_off
        self.el_gain = el_gain
        self.az_gain = az_gain
        self.tilt_az = tilt_az
        self.tilt_angle = tilt_angle
        self.lateral_tilt = lateral_tilt

    @staticmethod
    def from_dict(data):
        return AzElRotator(**data)

    def to_dict(self):
        return {key: float(getattr(self, key)) for key in ('el_off', 'az_off', 'el_gain', 'az_gain',
                                                           'tilt_az', 'tilt_angle', 'lateral_tilt')}

    @classmethod
    def load(cls, filename):
        with open(filename, 'r') as fh:
            obj = cls.from_dict(yaml.safe_load(fh))
        return obj

    def save(self, filename):
        with open(filename, 'w') as fh:
            yaml.dump(self.to_dict(), fh)

    @property
    def payload_q(self) -> np.quaternion:
        """
        quaternion representing the payload tilt
        """
        return self._payload_q(self.lateral_tilt)

    @property
    def platform_q(self) -> np.quaternion:
        """
        quaternion representing the platform tilt
        """
        return self._platform_q(self.tilt_az, self.tilt_angle)

    @staticmethod
    @lru_cache(maxsize=128)
    def _payload_q(lateral_tilt) -> np.quaternion:
        return eul_to_q((np.deg2rad(lateral_tilt),), 'z')

    @staticmethod
    @lru_cache(maxsize=128)
    def _platform_q(tilt_az, tilt_angle) -> np.quaternion:
        tilt_axis = q_times_v(eul_to_q((np.deg2rad(tilt_az - 90),), 'z'), np.array([1, 0, 0]))
        return quaternion.from_rotation_vector(tilt_axis * np.deg2rad(tilt_angle))

    def to_real(self, az, el, az_dot=None, el_dot=None, wrap=False):
        # Assumes x-axis points to the north, y-axis to the east and z-axis down (az=0 is north, el=0 is horizon)

        # remove the effect of offsets and gains
        az_m = np.deg2rad(wrapdeg((az - self.az_off) / self.az_gain))
        el_m = np.deg2rad((el - self.el_off) / self.el_gain)

        q_m = eul_to_q((az_m, el_m), 'zy')

        q_r = self.platform_q * q_m * self.payload_q
        az_r, el_r = to_azel(q_r)

        if not wrap:
            az_r = (az_r + 360) if abs(az_r - az) > 180 else az_r

        if az_dot is not None:
            # as in https://ahrs.readthedocs.io/en/latest/filters/angular.html
            omega_m = np.quaternion(0, 0, np.deg2rad(el_dot)/self.el_gain, np.deg2rad(az_dot)/self.az_gain)
            q_m_dot = 0.5 * omega_m * q_m
            q_r_dot = self.platform_q * q_m_dot * self.payload_q
            omaga_r = 2 * q_r_dot * q_r.conj()
            az_dot_m = np.rad2deg(omaga_r.z)
            el_dot_m = np.rad2deg(omaga_r.y)
            return (az_m, el_m), (az_dot_m, el_dot_m)

        return az_r, el_r

    def to_motor(self, az, el, az_dot=None, el_dot=None, wrap=False):
        # Assumes x-axis points to the north, y-axis to the east and z-axis down (az=0 is north, el=0 is horizon)
        q_r = eul_to_q((np.deg2rad(az), np.deg2rad(el)), 'zy')

        # FIXME: there's something wrong with to_motor as --debug-model returns errors

        q_m = self.platform_q.conj() * q_r * self.payload_q.conj()
        # q_m = self.payload_q.conj() * q_r * self.platform_q.conj()
        az_m, el_m = to_azel(q_m)

        # add the effect of offsets and gains
        az_m = wrapdeg(az_m * self.az_gain + self.az_off)
        el_m = el_m * self.el_gain + self.el_off

        if not wrap:
            az_m = (az_m + 360) if abs(az_m - az) > 180 else az_m

        if az_dot is not None:
            # as in https://ahrs.readthedocs.io/en/latest/filters/angular.html
            omega_r = np.quaternion(0, 0, np.deg2rad(el_dot), np.deg2rad(az_dot))
            q_r_dot = 0.5 * omega_r * q_r
            q_m_dot = self.platform_q.conj() * q_r_dot * self.payload_q.conj()
            omaga_m = 2 * q_m_dot * q_m.conj()
            az_dot_m = self.az_gain * np.rad2deg(omaga_m.z)
            el_dot_m = self.el_gain * np.rad2deg(omaga_m.y)
            return (az_m, el_m), (az_dot_m, el_dot_m)

        return az_m, el_m

    def __str__(self):
        return f'AzElRotator(el_off={self.el_off:.3f}, az_off={self.az_off:.3f}, el_gain={self.el_gain:.4f}, ' \
               f'az_gain={self.az_gain:.4f}, tilt_az={self.tilt_az:.4f}, tilt_angle={self.tilt_angle:.4f}, ' \
               f'lateral_tilt={self.lateral_tilt:.4f})'


def wrapdeg(angle):
    return (angle + 180) % 360 - 180


def eul_to_q(angles, order='xyz', reverse=False):
    """ combine euler rotations using the body-fixed convention """
    assert len(angles) == len(order), 'len(angles) != len(order)'
    q = quaternion.one
    idx = {'x': 0, 'y': 1, 'z': 2}
    for angle, axis in zip(angles, order):
        w = math.cos(angle / 2)
        v = [0, 0, 0]
        v[idx[axis]] = math.sin(angle / 2)
        dq = np.quaternion(w, *v)
        q = (dq * q) if reverse else (q * dq)
    return q


def q_times_v(q, v):
    """ rotate vector v by quaternion q """
    qv = np.quaternion(0, *v)
    return (q * qv * q.conj()).vec


def to_azel(q):
    """ convert quaternion to azimuth and elevation """
    yaw, pitch, roll = to_ypr(q)
    return np.rad2deg(yaw), np.rad2deg(pitch)


def to_ypr(q):
    # from https://math.stackexchange.com/questions/687964/getting-euler-tait-bryan-angles-from-quaternion-representation
    q0, q1, q2, q3 = quaternion.as_float_array(q)
    roll = np.arctan2(q2 * q3 + q0 * q1, .5 - q1 ** 2 - q2 ** 2)
    pitch = np.arcsin(np.clip(-2 * (q1 * q3 - q0 * q2), -1, 1))
    yaw = np.arctan2(q1 * q2 + q0 * q3, .5 - q2 ** 2 - q3 ** 2)
    return yaw, pitch, roll


def to_spherical(x, y, z):
    r = math.sqrt(x ** 2 + y ** 2 + z ** 2)
    theta = math.acos(z / r)
    phi = math.atan2(y, x)
    lat = math.pi / 2 - theta
    lon = phi
    return lat, lon, r


if __name__ == '__main__':
    # Call geometry.py directly for model calibration. See ArgParser help for the parameters.
    # Parameters el_off, az_off, el_gain and az_gain can be set to 0, 0, 1, 1 respectively, if rotator offset and
    # pulses per degree are adjusted in the controller instead.
    main()
