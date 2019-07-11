#!/usr/bin/python

# This code is derived from the work of BehindTheSciences.com and Brian Lavery

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so.

import datetime
import fcntl
import hashlib
import json
import os
import psutil
import requests
import select
import socket
import spidev
import subprocess
import sys
import time
import traceback
import uuid

import RPi.GPIO as GPIO

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

import configcache
import filequeue
from lib_tft24T import TFT24T
import util


# For LCD TFT GPIOs. Numbering is GPIO.
DC = 26
RST = 25
LED = 23

# Constants
ICON_SIZE = 30


def new_icon(icon, font, inset):
    img = Image.new('RGBA', (320, 240))
    img_writer = ImageDraw.Draw(img)
    width, height = img_writer.textsize(icon, font=font)
    img_writer.text(((ICON_SIZE - width) / 2 + 5, inset + 5),
                    icon, fill='black', font=font)
    util.log('Loaded %s icon' % icon_name)
    return img


util.log('Started')

# Make sure we're using supported hardware
product, version = util.hardware_ident()
util.log('Hardware product is: "%s"' % product)
util.log('Hardware version is: "%s"' % version)
if product != 'GangScan':
    util.log('Hardware product unsupported, aborting.')
    sys.exit(1)
if version != '0x0008':
    util.log('Hardware version unsupported, aborting.')
    sys.exit(1)

# Setup GPIO
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# Initialize display.
TFT = TFT24T(spidev.SpiDev(), GPIO, landscape=False)
TFT.initLCD(DC, RST, LED)
TFT.clear((125, 255, 125))
util.log('Initialized screen')

# Update clock
try:
    subprocess.check_output('sudo ntpdate -s time.nist.gov', shell=True)
    util.log('Updated the clock')
except:
    # This will fail if we don't have network connectivity
    pass

# Find old RFID reader processes and terminate them at lot
for proc in psutil.process_iter():
    try:
        pinfo = proc.as_dict(attrs=['pid', 'cmdline', 'username'])
    except psutil.NoSuchProcess:
        # Process ended before we got to kill it!
        pass
    else:
        if pinfo['cmdline'] == ['/usr/bin/python3', 'read_rfid.py']:
            util.log('Found stale process: %s' % pinfo)
            os.kill(pinfo['pid'], 9)

# Start listening for gangserver announcements
announce = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
announce.bind(('', 5000))

# Configuration
cm = configcache.ConfigManager()

# Create the file queue
queue = filequeue.FileQueue(
    os.path.expanduser('~/gangscan-%s' % cm.get('device-name', 'unknown')))

# Objects we need to draw things
icons = ImageFont.truetype('gangscan/materialdesignicons-webfont.ttf',
                           ICON_SIZE)
text = ImageFont.truetype('gangscan/BebasNeue.ttf', ICON_SIZE)
small_text = ImageFont.truetype('gangscan/BebasNeue.ttf', int(ICON_SIZE / 2))
medium_text = ImageFont.truetype('gangscan/BebasNeue.ttf', int(ICON_SIZE * 1))
giant_text = ImageFont.truetype('gangscan/BebasNeue.ttf', int(ICON_SIZE * 1.3))

images = {}
images['logo'] = Image.open('gangscan/gangscan.jpeg').convert('RGBA')
images['logo'] = images['logo'].resize((320, 240))
util.log('Loaded logo')

for (icon_name, icon, font, inset) in [
        ('wifi_on', chr(0xf5a9), icons, 0),
        ('wifi_off', chr(0xf5aa), icons, 0),
        ('connect_on', chr(0xf1f5), icons, ICON_SIZE + 5),
        ('connect_off', chr(0xf1f8), icons, ICON_SIZE),
        ('location', cm.get('location', '???'), text, (ICON_SIZE + 5) * 2)]:
    images[icon_name] = new_icon(icon, font, inset)
    
# Start the RFID reader process
(pipe_read, pipe_write) = os.pipe()
reader = subprocess.Popen(('/usr/bin/python3 gangscan/read_rfid.py '
                           '--presharedkey=%s --linger=1'
                           % cm.get('pre-shared-key', '')),
                          shell=True, stdout=pipe_write, stderr=pipe_write)

# Make the reader non-blocking
flag = fcntl.fcntl(pipe_read, fcntl.F_GETFL)
fcntl.fcntl(pipe_read, fcntl.F_SETFL, flag | os.O_NONBLOCK)

last_scanned = None
last_scanned_time = 0
last_status_time = 0

connected = False
last_netcheck_time = 0

def draw_status():
    global last_status_time

    status = images['logo']

    # Paint status icons
    if cm.get('ipaddress') != '...':
        status = Image.alpha_composite(status, images['wifi_on'])
    else:
        status = Image.alpha_composite(status, images['wifi_off'])

    if connected:
        status = Image.alpha_composite(status, images['connect_on'])
    else:
        status = Image.alpha_composite(status, images['connect_off'])

    status = Image.alpha_composite(status, images['location'])

    now = datetime.datetime.now()
    status_writer = ImageDraw.Draw(status)

    # Display time
    status_writer.text((5, 240 - (ICON_SIZE / 2) - 5),
                       '%02d:%02d' % (now.hour, now.minute),
                       fill='black',
                       font=small_text)

    # Display queue size
    queued_string = '%d queued' % queue.count_events('new')
    width, height = status_writer.textsize(
        queued_string,
        font=small_text)
    status_writer.text((320 - width - 5, 240 - (ICON_SIZE / 2) - 5),
                       queued_string,
                       fill='black',
                       font=small_text)

    # Display network address
    width, height = status_writer.textsize(cm.get('ipaddress'), font=small_text)
    status_writer.text(((320 - width) / 2, 240 - (ICON_SIZE / 2) - 5),
                       cm.get('ipaddress'),
                       fill='black',
                       font=small_text)

    # Display recently scanned person
    if time.time() - last_scanned_time < cm.get('name-linger', 5):
        font = giant_text
        if len(last_scanned) > 20:
            font = medium_text

        width, height = status_writer.textsize(
            last_scanned, font=font)
        status_writer.rectangle(
            (160 - width / 2 - 5, 120 - height / 2 - 5,
             160 + width / 2 + 5, 120 + height / 2 + 5),
            fill='white')

        fill='green'
        if last_scanned == '???':
            fill='red'

        status_writer.text(
            ((320 - width) / 2, (240 - height) / 2),
            last_scanned,
            fill=fill,
            font=font)

    last_status_time = time.time()
    TFT.display(status.rotate(90, resample=0, expand=1))
    util.uptime()

reader_buffer = ''
try:
    while reader.poll() is None:
        if connected:
            event_id = queue.get_event('new')
            if event_id:
                data = queue.read_event('new', event_id)
                data['timestamp-transferred'] = time.time()
                try:
                    r = requests.put('http://%s:%d/event/%s'
                                     %(cm.get('server_address'),
                                       cm.get('server_port'),
                                       event_id),
                                     data={'data': json.dumps(data)})
                    if r.status_code == 200:
                        util.log('Wrote queued event %s' % event_id)
                        queue.change_state('new', 'sent', event_id)
                except Exception as e:
                    # Failed to stream event to server
                    util.log('Failed to stream queued event %s: %s'
                             %(event_id, e))
                    connected = False

        readable = select.select([pipe_read, announce], [], [], 0)[0]
        if pipe_read in readable:
            reader_buffer += os.read(pipe_read, 8192).decode('utf-8')
            util.log('RFID data queued: %s' % reader_buffer)

            scans = reader_buffer.split('\n')
            reader_buffer = ''
            for scan in scans:
                util.log('RFID scan: "%s"' % scan)

            if len(scans[-1]) > 0:
                reader_buffer = scans[-1]
            util.log('RFID data queued: %s' % reader_buffer)

            for scan in scans[:-1]:
                if scan[0] == 'E':
                    last_scanned = '???'
                    last_scanned_time = time.time()
                    last_status_time = 0
                    continue

                try:
                    data = json.loads(scan)
                    if data['outcome']:
                        last_scanned = data['owner']
                        last_scanned_time = time.time()
                        last_status_time = 0

                        event_id = str(uuid.uuid4())
                        data['event_id'] = event_id
                        data['location'] = cm.get('location')
                        data['device'] = cm.get('device-name', 'unknown')
                        data['timestamp-device'] = time.time()

                        h = hashlib.sha256()
                        for key in data:
                            s = '%s:%s' %(key, data[key])
                            h.update(s.encode('utf-8'))
                        data['signature'] = h.hexdigest()

                        queue.store_event('new', event_id, data)

                        util.log('Forced update of status screen')
                        draw_status()

                except Exception as e:
                    util.log('Ignoring malformed data: %s' % e)
                    print('-' * 60)
                    traceback.print_exc(file=sys.stdout)
                    print('-' * 60)
                    sys.stdout.flush()

        elif announce in readable:
            try:
                data = announce.recvfrom(100)[0].decode('utf-8')
                util.log('Received gangserver announcement: %s' % data)
                server_address_port = data.split(' ')[1]
                server_address, server_port = server_address_port.split(':')
                server_port = int(server_port)
                connected = True

                cm.set('server_address', server_address)
                cm.set('server_port', server_port)
            except Exception as e:
                util.log('Announcement error: %s' % e)

        elif time.time() - last_netcheck_time > 30:
            # Determine IP address
            last_netcheck_time = time.time()
            old_location = cm.get('location')

            cm.heartbeat()

            if cm.get('location') != old_location:
                images['location'] = new_icon(cm.get('location'), text,
                                              (ICON_SIZE + 5) * 2)

        elif time.time() - last_status_time > 5:
            util.log('Updating status screen')
            draw_status()

    # The RFID reader process exitted?
    os.close(pipe_read)
    os.close(pipe_write)
    util.log('The RFID reader process exitted with code %d'
             % reader.returncode)

finally:
    TFT.clear((255, 125, 125))
