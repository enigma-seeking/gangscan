#!/usr/bin/python3

import argparse
import datetime
import hashlib
import json
import signal
import time
import sys

import RPi.GPIO as GPIO
from pirc522 import RFID

rdr = RFID(bus=0, device=1, pin_rst=16, pin_irq=19, pin_ce=7, pin_mode=GPIO.BCM)
rdr.set_antenna_gain(6)
rdr.init()
util = rdr.util()
util.debug = False

parser = argparse.ArgumentParser()
parser.add_argument('--presharedkey')
parser.add_argument('--linger', type=int)
parser.add_argument('--debug', dest='debug', action='store_true')
args = parser.parse_args()


def end_read(signal,frame):
    global run
    print("\nCtrl+C captured, ending read.")
    run = False
    rdr.cleanup()
    sys.exit()

signal.signal(signal.SIGINT, end_read)


def uid_to_num(uid):
    n = 0
    for i in range(0, 5):
        n = n * 256 + uid[i]
    return n


def output(d):
    print(json.dumps(d, sort_keys=True))
    sys.stdout.flush()


def log(s):
    if args.debug:
        output({'when': str(datetime.datetime.now()),
                'log': s,
                'outcome': False})


last_read = None
last_read_time = 0
reader = None


run = True
while run:
    # Wait for a tag to appear
    rdr.wait_for_tag()
    log('Detected card')

    # There might be more than one card read per IRQ?
    while True:
        # Read the tag
        (error, data) = rdr.request()
        if error:
            log('Reader error')
            break

        (error, uid) = rdr.anticoll()
        if error:
            log('Collision error')
            continue

        cardid = uid_to_num(uid)

        util.set_tag(uid)
        util.auth(rdr.auth_a, [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
        log('Authenticate')

        text = ''
        for block in [8, 9, 10]:
            error = util.do_auth(block)
            if not error:
                error, data = util.rfid.read(block)
                for c in data:
                    text += chr(c)
        util.deauth()
        log('Read and deauth')

        if len(text.rstrip(' ')) == 0:
            log('No data read')
            continue

        # Verify the read data
        try:
            owner, sig = text.rstrip(' ').split(',')

            h = hashlib.sha256()
            h.update(owner.encode('utf-8'))
            h.update(str(cardid).encode('utf-8'))
            h.update(args.presharedkey.encode('utf-8'))
            s = h.hexdigest()[-6:]

            outcome = True
            if s != sig:
                outcome = False

            data = {'cardid': cardid,
                    'owner': owner,
                    'sha': sig,
                    'outcome': outcome}

            if last_read != data:
                output(data)
                last_read = data
                last_read_time = time.time()

            elif time.time() - last_read_time > args.linger - 1:
                last_read = None
                last_read_time = time.time()

        except Exception as e:
            log('Exception: %s' % e)
