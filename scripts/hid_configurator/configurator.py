#
# Copyright (c) 2019 Nordic Semiconductor ASA
#
# SPDX-License-Identifier: LicenseRef-BSD-5-Clause-Nordic

import os
import sys
import hid
import struct
import time
import zlib

import argparse
import logging
import collections

from enum import IntEnum

ConfigOption = collections.namedtuple('ConfigOption', 'range event_id help')

REPORT_ID = 5
REPORT_SIZE = 30
EVENT_DATA_LEN_MAX = REPORT_SIZE - 6

TYPE_FIELD_POS = 0
GROUP_FIELD_POS = 6
EVENT_GROUP_SETUP = 0x1
EVENT_GROUP_DFU = 0x2

MOD_FIELD_POS = 3
SETUP_MODULE_SENSOR = 0x1
SETUP_MODULE_LED = 0x1

OPT_FIELD_POS = 0
SENSOR_OPT_CPI = 0x0
SENSOR_OPT_DOWNSHIFT_RUN = 0x1
SENSOR_OPT_DOWNSHIFT_REST1 = 0x2
SENSOR_OPT_DOWNSHIFT_REST2 = 0x3

DFU_START = 0x0
DFU_DATA = 0x1
DFU_SYNC = 0x2
DFU_REBOOT = 0x3
DFU_IMGINFO = 0x4

FLASH_PAGE_SIZE = 4096

POLL_INTERVAL = 0.02
POLL_RETRY_COUNT = 200

DFU_SYNC_RETRIES = 3
DFU_SYNC_INTERVAL = 1


PMW3360_OPTIONS = {
    'downshift_run':    ConfigOption((10,   2550),   SENSOR_OPT_DOWNSHIFT_RUN,   'Run to Rest 1 switch time [ms]'),
    'downshift_rest1':  ConfigOption((320,  81600),  SENSOR_OPT_DOWNSHIFT_REST1, 'Rest 1 to Rest 2 switch time [ms]'),
    'downshift_rest2':  ConfigOption((3200, 816000), SENSOR_OPT_DOWNSHIFT_REST2, 'Rest 2 to Rest 3 switch time [ms]'),
    'cpi':              ConfigOption((100,  12000),  SENSOR_OPT_CPI,             'CPI resolution'),
}

PAW3212_OPTIONS = {
    'sleep1_timeout':  ConfigOption((32,    512),    SENSOR_OPT_DOWNSHIFT_RUN,   'Sleep 1 switch time [ms]'),
    'sleep2_timeout':  ConfigOption((20480, 327680), SENSOR_OPT_DOWNSHIFT_REST1, 'Sleep 2 switch time [ms]'),
    'sleep3_timeout':  ConfigOption((20480, 327680), SENSOR_OPT_DOWNSHIFT_REST2, 'Sleep 3 switch time [ms]'),
    'cpi':             ConfigOption((0,     2394),   SENSOR_OPT_CPI,             'CPI resolution'),
}

PCA20041_CONFIG = {
    'sensor' : {
        'id' : SETUP_MODULE_SENSOR,
        'options' : PMW3360_OPTIONS
    }
}

PCA20044_CONFIG = {
    'sensor' : {
        'id' : SETUP_MODULE_SENSOR,
        'options' : PAW3212_OPTIONS
    }
}

PCA20045_CONFIG = {
    'sensor' : {
        'id' : SETUP_MODULE_SENSOR,
        'options' : PAW3212_OPTIONS
    }
}

DEVICE = {
    'desktop_mouse_nrf52832' : {
        'vid' : 0x1915,
        'pid' : 0x52DA,
        'config' : PCA20044_CONFIG
    },
    'desktop_mouse_nrf52810' : {
        'vid' : 0x1915,
        'pid' : 0x52DB,
        'config' : PCA20045_CONFIG
    },
    'gaming_mouse' : {
        'vid' : 0x1915,
        'pid' : 0x52DE,
        'config' : PCA20041_CONFIG
    },
    'keyboard' : {
        'vid' : 0x1915,
        'pid' : 0x52DD,
        'config' : None
    },
    'dongle' : {
        'vid' : 0x1915,
        'pid' : 0x52DC,
        'config' : None
    }
}


class ConfigStatus(IntEnum):
    SUCCESS            = 0
    PENDING            = 1
    FETCH              = 2
    TIMEOUT            = 3
    REJECT             = 4
    WRITE_ERROR        = 5
    DISCONNECTED_ERROR = 6
    FAULT              = 99

class Response(object):
    def __init__(self, recipient, event_id, status, data):
        self.recipient = recipient
        self.event_id  = event_id
        self.status    = ConfigStatus(status)
        self.data      = data

    def __repr__(self):
        base_str = ('Response:\n'
                    '\trecipient 0x{:04x}\n'
                    '\tevent_id  0x{:02x}\n'
                    '\tstatus    {}\n').format(self.recipient,
                                               self.event_id,
                                               str(self.status))
        if self.data is None:
            data_str = '\tno data'
        else:
            data_str = ('\tdata_len {}\n'
                        '\tdata {}\n').format(len(self.data), self.data)

        return base_str + data_str

    @staticmethod
    def parse_response(response_raw):
        data_field_len = len(response_raw) - struct.calcsize('<BHBBB')

        if data_field_len < 0:
            logging.error('Response too short')
            return None

        # Report ID is not included in the feature report from device
        fmt = '<BHBBB{}s'.format(data_field_len)

        (report_id, rcpt, event_id, status, data_len, data) = struct.unpack(fmt, response_raw)

        if report_id != REPORT_ID:
            logging.error('Improper report ID')
            return None

        if data_len > len(data):
            logging.error('Required data not present')
            return None

        if data_len == 0:
            event_data = None
        else:
            event_data = data[:data_len]

        return Response(rcpt, event_id, status, event_data)


def progress_bar(permil):
    LENGTH = 40
    done_len = LENGTH * permil // 1000
    progress_line = '[' + '*' * done_len + '-' * (LENGTH - done_len) + ']'
    percent = permil / 10.0
    print('\r{} {}%'.format(progress_line, percent), end='')


def check_range(value, value_range):
    if value > value_range[1] or value < value_range[0]:
        return False
    return True


def create_set_report(recipient, event_id, event_data):
    """ Function creating a report in order to set a specified configuration
        value.
        Recipient is a device product ID. """

    assert(type(recipient) == int)
    assert(type(event_id) == int)
    if event_data:
        assert(type(event_data) == bytes)
        event_data_len = len(event_data)

    status = ConfigStatus.PENDING
    report = struct.pack('<BHBBB', REPORT_ID, recipient, event_id, status,
                         event_data_len)
    if event_data:
        report += event_data

    assert(len(report) <= REPORT_SIZE)
    report += b'\0' * (REPORT_SIZE - len(report))

    return report


def create_fetch_report(recipient, event_id):
    """ Function for creating a report which requests fetching of
        a configuration value from a device.
        Recipient is a device product ID. """

    assert(type(recipient) == int)
    assert(type(event_id) == int)

    status = ConfigStatus.FETCH
    report = struct.pack('<BHBBB', REPORT_ID, recipient, event_id, status, 0)

    assert(len(report) <= REPORT_SIZE)
    report += b'\0' * (REPORT_SIZE - len(report))

    return report


def exchange_feature_report(dev, recipient, event_id, event_data, is_fetch):
    if is_fetch:
        data = create_fetch_report(recipient, event_id)
    else:
        data = create_set_report(recipient, event_id, event_data)

    dev.send_feature_report(data)

    for retry in range(POLL_RETRY_COUNT):
        time.sleep(POLL_INTERVAL)

        response_raw = dev.get_feature_report(REPORT_ID, REPORT_SIZE)
        response = Response.parse_response(response_raw)

        if response is None:
            logging.error('Invalid response')
            op_status = ConfigStatus.FAULT
            break

        logging.debug('Parsed response: {}'.format(response))

        if (response.recipient != recipient) or (response.event_id != event_id):
            logging.error('Response does not match the request:\n'
                          '\trequest: recipient {} event_id {}\n'
                          '\tresponse: recipient {}, event_id {}'.format(recipient, event_id,
                                                                         response.recipient, response.event_id))
            op_status = ConfigStatus.FAULT
            break

        op_status = response.status
        if op_status != ConfigStatus.PENDING:
            break

    fetched_data = None
    success = False
    if op_status == ConfigStatus.SUCCESS:
        logging.info('Success')
        success = True
        if is_fetch:
            fetched_data = response.data
    else:
        logging.warning('Error: {}'.format(op_status.name))

    if is_fetch:
        return success, fetched_data
    return success


def get_device_pid(device_type):
    return DEVICE[device_type]['pid']


def get_device_vid(device_type):
    return DEVICE[device_type]['vid']


def open_device(device_type):
    dev = None

    devlist = [device_type]
    if 'dongle' not in devlist:
        devlist.append('dongle')

    for i in devlist:
        try:
            vid=get_device_vid(i)
            pid=get_device_pid(i)
            dev = hid.Device(vid=vid, pid=pid)
            break
        except hid.HIDException:
            pass
        except:
            logging.error('Unknown error: {}'.format(sys.exc_info()[0]))

    if dev is None:
        print('Cannot find selected device nor dongle')
    elif i == device_type:
        print('Device found')
    else:
        print('Device connected via {}'.format(i))

    return dev


def dfu_sync(dev, args):
    event_id = (EVENT_GROUP_DFU << GROUP_FIELD_POS) | (DFU_SYNC << TYPE_FIELD_POS)
    recipient = get_device_pid(args.device_type)

    try:
        success, fetched_data = exchange_feature_report(dev, recipient, event_id, None, True)
    except:
        success = False

    if not success:
        return None

    fmt = '<BIII'
    assert(struct.calcsize(fmt) <= EVENT_DATA_LEN_MAX)

    if (fetched_data is None) or (len(fetched_data) < struct.calcsize(fmt)):
        return None

    return struct.unpack(fmt, fetched_data)


def dfu_start(dev, args, img_length, img_csum, offset):
    event_id = (EVENT_GROUP_DFU << GROUP_FIELD_POS) | (DFU_START << TYPE_FIELD_POS)
    recipient = get_device_pid(args.device_type)

    event_data = struct.pack('<III', img_length, img_csum, offset)

    try:
        success = exchange_feature_report(dev, recipient, event_id, event_data, False)
    except:
        success = False

    return success


def file_crc(dfu_image):
    crc32 = 1

    img_file = open(dfu_image, 'rb')
    if img_file is None:
        return None

    while True:
        chunk_data = img_file.read(512)
        if len(chunk_data) == 0:
            break;
        crc32 = zlib.crc32(chunk_data, crc32)

    img_file.close()

    return crc32


def dfu_sync_wait(dev, args, is_active):
    if is_active:
        dfu_state = 0x01
    else:
        dfu_state = 0x00

    for i in range(DFU_SYNC_RETRIES):
        dfu_info = dfu_sync(dev, args)
        if dfu_info is not None:
            if dfu_info[0] != dfu_state:
                # DFU may be transiting its state. This can happen when previous
                # interrupted DFU operation is timing out. Sleep to allow it
                # to settle the state.
                time.sleep(DFU_SYNC_INTERVAL)
            else:
                break
    return dfu_info


def perform_dfu(dev, args):
    dfu_image = args.dfu_image
    event_id = (EVENT_GROUP_DFU << GROUP_FIELD_POS) | (DFU_DATA << TYPE_FIELD_POS)
    recipient = get_device_pid(args.device_type)

    if not os.path.isfile(dfu_image):
        print('DFU image file does not exists')
        return

    img_length = os.stat(dfu_image).st_size
    if img_length <= 0:
        print('DFU image is empty')
        return

    print('DFU image size: {} bytes'.format(img_length))

    # Check there is no other DFU operation.
    dfu_info = dfu_sync_wait(dev, args, False)
    if dfu_info is None:
        print('Cannot start DFU, device not responding')
        return

    if dfu_info[0] != 0:
        print('Cannot start DFU, already in progress')
        return

    # Check if the previously interrupted DFU operation can be resumed.
    img_csum = file_crc(dfu_image)
    if (dfu_info[1] == img_length) and (dfu_info[2] == img_csum) and (dfu_info[3] <= img_length):
        print('Resume DFU at {}'.format(dfu_info[3]))
        offset = dfu_info[3]
    else:
        offset = 0

    # Start DFU operation at selected offset.
    # It can happen that device will reject this request - this will be
    # verified by dfu sync at data exchange.
    success = dfu_start(dev, args, img_length, img_csum, offset)
    if not success:
        print('Cannot start DFU operation')

    img_file = open(dfu_image, 'rb')
    img_file.seek(offset)

    try:
        while offset < img_length:
            if offset % FLASH_PAGE_SIZE == 0:
                # Sync DFU state at regular intervals to ensure everything
                # is all right.
                success = False
                dfu_info = dfu_sync(dev, args)
                if dfu_info is None:
                    print('Lost communication with the device')
                    break
                if dfu_info[0] == 0:
                    print('DFU interrupted by device')
                    break
                if (dfu_info[1] != img_length) or (dfu_info[2] != img_csum) or (dfu_info[3] != offset):
                    print('Invalid sync information')
                    break

            chunk_data = img_file.read(EVENT_DATA_LEN_MAX)
            chunk_len = len(chunk_data)
            if chunk_len == 0:
                break

            logging.debug('Send DFU request: offset {}, size {}'.format(offset, chunk_len))

            progress_bar(int(offset/img_length * 1000))
            try:
                success = exchange_feature_report(dev, recipient, event_id, chunk_data, False)
            except:
                success = False
            if not success:
                print('Lost communication with the device')
                break

            offset += chunk_len
    except:
        success = False

    img_file.close()
    print('')

    if success:
        print('DFU transfer completed')
        success = False

        dfu_info = dfu_sync_wait(dev, args, False)
        if dfu_info is None:
            print('Lost communication with the device')
        else:
            if dfu_info[0] != 0:
                print('Device holds DFU active')
            elif dfu_info[3] != offset:
                print('Device holds incorrect image offset')
            else:
                success = perform_fwreboot(dev, args)

    if not success:
        print('DFU transfer failed')
    else:
        print('DFU completed')


def perform_config(dev, args):
    module_config = DEVICE[args.device_type]['config'][args.module]
    module_id = module_config['id']
    options = module_config['options']

    config_name  = args.option
    config_opts  = options[config_name]

    value_range  = config_opts.range
    opt_id       = config_opts.event_id

    recipient = get_device_pid(args.device_type)
    event_id = (EVENT_GROUP_SETUP << GROUP_FIELD_POS) | (module_id << MOD_FIELD_POS) | (opt_id << OPT_FIELD_POS)

    if args.value is None:
        logging.debug('Fetch the current value of {} from the firmware'.format(config_name))
        try:
            success, fetched_data = exchange_feature_report(dev, recipient, event_id, None, True)
        except:
            success = False

        if success and fetched_data:
            val = int.from_bytes(fetched_data, byteorder='little')
            print('Fetched {}: {}'.format(config_name, val))
        else:
            print('Failed to fetch {}'.format(config_name))
    else:
        config_value = int(args.value)
        logging.debug('Send request to update {}: {}'.format(config_name, config_value))
        if not check_range(config_value, value_range):
            print('Failed. Config value for {} must be in range {}'.format(config_name, value_range))
            return

        event_data = struct.pack('<I', config_value)
        try:
            success = exchange_feature_report(dev, recipient, event_id, event_data, False)
        except:
            success = False

        if success:
            print('{} set to {}'.format(config_name, config_value))
        else:
            print('Failed to set {}'.format(config_name))


def perform_fwinfo(dev, args):
    event_id = (EVENT_GROUP_DFU << GROUP_FIELD_POS) | (DFU_IMGINFO << TYPE_FIELD_POS)
    recipient = get_device_pid(args.device_type)

    try:
        success, fetched_data = exchange_feature_report(dev, recipient, event_id, None, True)
    except:
        success = False

    if success and fetched_data:
        fmt = '<BIBBHI'
        assert(struct.calcsize(fmt) <= EVENT_DATA_LEN_MAX)

        (flash_area_id, image_len, ver_major, ver_minor, ver_rev, ver_build_nr) = struct.unpack(fmt, fetched_data)
        print(('Firmware info\n'
               '\tFLASH area id: {}\n'
               '\tImage length: {}\n'
               '\tVersion: {}.{}.{}.{}').format(flash_area_id,
                                                image_len,
                                                ver_major,
                                                ver_minor,
                                                ver_rev,
                                                ver_build_nr))
    else:
        print('FW info request failed')


def perform_fwreboot(dev, args):
    event_id = (EVENT_GROUP_DFU << GROUP_FIELD_POS) | (DFU_REBOOT << TYPE_FIELD_POS)
    recipient = get_device_pid(args.device_type)

    try:
        success, fetched_data = exchange_feature_report(dev, recipient, event_id, None, True)
    except:
        success = False

    if success:
        print('Firmware rebooted')
    else:
        print('FW reboot request failed')

    return success


def configurator():
    logging.basicConfig(level=logging.ERROR)
    logging.info('Configuration channel for nRF52 Desktop')

    parser = argparse.ArgumentParser()
    sp_devices = parser.add_subparsers(dest='device_type')
    sp_devices.required = True
    for device_name in DEVICE:
        device_parser = sp_devices.add_parser(device_name)

        sp_commands = device_parser.add_subparsers(dest='command')
        sp_commands.required = True

        parser_dfu = sp_commands.add_parser('dfu', help='Run DFU')
        parser_dfu.add_argument('dfu_image', type=str, help='Path to a DFU image')

        sp_commands.add_parser('fwinfo', help='Obtain information about FW image')
        sp_commands.add_parser('fwreboot', help='Request FW reboot')

        device_config = DEVICE[device_name]['config']
        if device_config is not None:
            assert(type(device_config) == dict)
            parser_config = sp_commands.add_parser('config', help='Configuration option get/set')

            sp_config = parser_config.add_subparsers(dest='module')
            sp_config.required = True

            for module_name in device_config:
                module_config = device_config[module_name]
                assert(type(module_config) == dict)
                module_opts = module_config['options']
                assert(type(module_opts) == dict)

                parser_config_module = sp_config.add_parser(module_name, help='{} module options'.format(module_name))
                sp_config_module = parser_config_module.add_subparsers(dest='option')
                sp_config_module.required = True

                for opt_name in module_opts:
                    parser_config_module_opt = sp_config_module.add_parser(opt_name, help=module_opts[opt_name].help)
                    parser_config_module_opt.add_argument('value', type=int, default=None, nargs='?', help='int from range {}'.format(module_opts[opt_name].range))
    args = parser.parse_args()

    dev = open_device(args.device_type)
    if not dev:
        return

    if args.command == 'dfu':
        perform_dfu(dev, args)
    elif args.command == 'fwinfo':
        perform_fwinfo(dev, args)
    elif args.command == 'fwreboot':
        perform_fwreboot(dev, args)
    elif args.command == 'config':
        perform_config(dev, args)

if __name__ == '__main__':
    try:
        configurator()
    except KeyboardInterrupt:
        pass
