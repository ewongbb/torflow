#!/bin/env python

from stem.util import enum

class Constants(object):

    tor_port = 9060
    tor_host = '127.0.0.1'

    control_port = 9061
    control_host = '127.0.0.1'
    control_pass = ""

    meta_port = 9052
    meta_host = '127.0.0.1'


# Types of "EVENT" message.
EVENT_TYPE = enum.Enum(
          CIRC="CIRC",
          STREAM="STREAM",
          ORCONN="ORCONN",
          STREAM_BW="STREAM_BW",
          BW="BW",
          NS="NS",
          NEWCONSENSUS="NEWCONSENSUS",
          BUILDTIMEOUT_SET="BUILDTIMEOUT_SET",
          GUARD="GUARD",
          NEWDESC="NEWDESC",
          ADDRMAP="ADDRMAP",
          DEBUG="DEBUG",
          INFO="INFO",
          NOTICE="NOTICE",
          WARN="WARN",
          ERR="ERR")

EVENT_STATE = enum.Enum(
          PRISTINE="PRISTINE",
          PRELISTEN="PRELISTEN",
          HEARTBEAT="HEARTBEAT",
          HANDLING="HANDLING",
          POSTLISTEN="POSTLISTEN",
          DONE="DONE")
