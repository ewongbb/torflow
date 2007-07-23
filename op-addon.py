#!/usr/bin/python

"""
  RWTH Aachen University, Informatik IV
  Copyright (C) 2007 Johannes Renner 
  Contact: renner <AT> i4.informatik.rwth-aachen.de
"""

import os
import re
import sys
import copy
import math
import time
import random
import socket
import threading
import Queue
import ConfigParser

from TorCtl import *
from TorCtl.TorUtil import plog, sort_list

## CONFIGURATION ##############################################################

# Set the version
VERSION = "0.0.01-alpha"

# Try to get the config-file from the commandline
if len(sys.argv) == 1:
  CONFIG_FILE = "pathrc.example"
elif len(sys.argv) == 2:
  CONFIG_FILE = sys.argv[1]
else: 
  plog("ERROR", "Too many arguments, exiting.")
  sys.exit(0)

# Set some defaults for string-variables that can be None
string_defaults = {"use_exit":None, "entry_country":None, "exit_country":None}
config = ConfigParser.SafeConfigParser(string_defaults)
if os.path.exists(CONFIG_FILE):
  plog("INFO", "Loading configuration from '" + CONFIG_FILE + "'")
  config.read(CONFIG_FILE)
else:
  plog("ERROR", "Config file '" + CONFIG_FILE + "' does not exist, exiting.")
  sys.exit(0)
  
# Sections
HOST_PORT = "HostPort"
CIRC_MANAGEMENT = "CircuitManagement"
NODE_SELECTION = "NodeSelection"
GEOIP = "GeoIP"
RTT = "RTT"

# Set global variables here
# Measure the circuits
measure_circs = config.getboolean(RTT, "measure_circs")
if measure_circs:
  import socks  
  # Hosts and ports to use for ping streams
  socks_host = config.get(RTT, "socks_host")
  socks_port = config.getint(RTT, "socks_port")
  # Choose randomly from a set of hosts/ports?
  ping_dummy_host = config.get(RTT, "ping_dummy_host")
  ping_dummy_port = config.getint(RTT, "ping_dummy_port")

  # Sleep interval between working loads in sec
  initial_interval = config.getfloat(RTT, "initial_interval")
  sleep_interval = config.getfloat(RTT, "sleep_interval")
  # Close a circ after n timeouts or avg measured slownesses
  timeout_limit = config.getint(RTT, "timeout_limit")
  # Close a circ after n measured slownesses
  slowness_limit = config.getint(RTT, "slowness_limit")
  # Close circs slower & create only circs faster than this
  slow = config.getfloat(RTT, "slow")

  # Set to True if we want to measure partial circuits
  # This also enables circuit creation from the model
  measure_partial_circs = config.getboolean(RTT, "measure_partial_circs")
  if measure_partial_circs:
    import networkx    
    # Minimum number of proposals to choose from
    min_proposals = config.getint(RTT, "min_proposals")
    # Min ratio of traditionally created circs
    # ensures growing of the explored subnet
    min_ratio = config.getfloat(RTT, "min_ratio")

  # Testing mode: Collect latencies of circuits and links in the 
  # network. Close circuits after num_tests measures and involve 
  # a FileHandler to write data to a file
  testing_mode = config.getboolean(RTT, "testing_mode")
  if testing_mode:
    num_tests = config.getint(RTT, "num_tests")
    num_records = config.getint(RTT, "num_records")

def get_geoip_config():
  """ Read the geoip-configuration from the config-file """
  # Check for GeoIP
  if config.getboolean(GEOIP, "use_geoip"):
    # Optional options
    unique_countries = None
    max_crossings = None
    if config.has_option(GEOIP, "unique_countries"):
      unique_countries = config.getboolean(GEOIP, "unique_countries")
    if config.has_option(GEOIP, "max_crossings"):
      max_crossings = config.getint(GEOIP, "max_crossings")
    path_config = GeoIPSupport.GeoIPConfig(
       unique_countries,
       max_crossings,
       entry_country = config.get(GEOIP, "entry_country"),
       exit_country = config.get(GEOIP, "exit_country"),
       excludes = None)
  else: path_config = None
  return path_config

# Configure the SelectionManager here!!
# Do NOT modify this object directly after it is handed to 
# PathBuilder, Use PathBuilder.schedule_selmgr instead.
__selmgr = PathSupport.SelectionManager(
      pathlen= config.getint(NODE_SELECTION, "pathlen"),
      order_exits = config.getboolean(NODE_SELECTION, "order_exits"),
      percent_fast = config.getint(NODE_SELECTION, "percent_fast"),
      percent_skip = config.getint(NODE_SELECTION, "percent_skip"),
      min_bw = config.getint(NODE_SELECTION, "min_bw"),
      use_all_exits = config.getboolean(NODE_SELECTION, "use_all_exits"),
      uniform = config.getboolean(NODE_SELECTION, "uniform"),
      use_exit = config.get(NODE_SELECTION, "use_exit"),
      use_guards = config.getboolean(NODE_SELECTION, "use_guards"),
      geoip_config = get_geoip_config())

## Connection #################################################################

class Connection(TorCtl.Connection):
  """ Connection-class that uses the RTTCircuit-class 
      TODO: add the CircuitClass to be used somewhere """
  def build_circuit(self, pathlen, path_sel):
    circ = Circuit()
    circ.path = path_sel.build_path(pathlen)
    circ.exit = circ.path[pathlen-1]
    circ.circ_id = self.extend_circuit(0, circ.id_path())
    return circ

  def build_circuit_from_path(self, path):
    """ Build circuit using a given path (= router-objects), 
        used to build circs from NetworkModel """
    circ = Circuit()
    circ.path = path
    circ.exit = path[len(path)-1]
    circ.circ_id = self.extend_circuit(0, circ.id_path())
    return circ

## Stats ######################################################################

class Stats:
  """ Statistics class that is used for recording stats """
  def __init__(self):
    self.values = []
    self.min = 0.0
    self.max = 0.0
    self.mean = 0.0
    self.dev = 0.0
    self.median = 0.0

  def add_value(self, value):
    # Append value
    self.values.append(value)
    # Set min & max
    if self.min == 0: self.min = value
    elif self.min > value: self.min = value
    if self.max < value: self.max = value
    # Refresh everything
    self.mean = self.get_mean()
    self.dev = self.get_dev()
    self.median = self.get_median()

  def get_mean(self):
    """ Compute mean from the values """
    if len(self.values) > 0:
      sum = reduce(lambda x, y: x+y, self.values, 0.0)
      return sum/len(self.values)
    else:
      return 0.0

  def get_dev(self):
    """ Return the stddev of the values """
    if len(self.values) > 1:
      mean = self.get_mean()
      sum = reduce(lambda x, y: x + ((y-mean)**2.0), self.values, 0.0)
      s = math.sqrt(sum/(len(self.values)-1))
      return s
    else:
      return 0.0

  def get_median(self):
    """ Return the median of the values """
    if len(self.values) > 0:
      self.values.sort()
      return self.values[(len(self.values)-1)/2]
    else: return 0.0

## FileHandler ################################################################

# TODO: Move this to TorCtl.TorUtil?
class FileHandler:
  """ FileHandler class for writing/appending collected data to a file """
  def __init__(self, filename):
    self.filename = filename

  def write(self, line):
    self.filehandle = open(self.filename, 'w')
    self.filehandle.write(line + "\n")
    self.filehandle.close() 

  def append(self, line):
    self.filehandle = open(self.filename, 'a')
    self.filehandle.write(line + "\n")
    self.filehandle.close() 
 
  def get_line_count(self):
    self.filehandle = open(self.filename)
    lines = self.filehandle.readlines()
    return len(lines)

## Circuit & Stream ###########################################################

class Circuit(PathSupport.Circuit): 
  """ Circuit class extended to RTTs and related stats """
  def __init__(self):
    PathSupport.Circuit.__init__(self)
    # RTT stuff
    self.part_rtts = {}		# dict of partial rtts, pathlen 3: 1-2-None
    self.current_rtt = None	# double (sec): current value
    self.stats = Stats()	# stats about total RTT contains history
    # Counters and flags
    self.age = 0		# age in rounds
    self.timeout_counter = 0	# timeout limit
    self.slowness_counter = 0 	# slowness limit
    self.rtt_created = False	# if this was created from the model
    # TODO: Move these to PathSupport.py?
    self.closed = False		# mark circuit closed
    self.extend_times = []      # list of all extend-times
    self.setup_duration = None  # sum of extend-times

  def add_rtt(self, rtt):
    """ Add a new value and refresh the stats """
    # Set current
    if self.current_rtt == None:
      self.current_rtt = rtt
    else:
      self.current_rtt = (self.current_rtt * 0.5) + (rtt * 0.5)
      plog("DEBUG", "Computing new current RTT from " + str(rtt) + " to " + 
         str(self.current_rtt))
    # Add new RTT to the stats
    self.stats.add_value(rtt)
    # Increase age
    self.age += 1

  def to_string(self):
    """ Create a current string representation """
    s = "Circuit " + str(self.circ_id) + ": "
    for r in self.path: s += " " + r.nickname + "(" + str(r.country_code) + ")"
    if not self.built: s += " (not yet built)"
    else: s += " (age=" + str(self.age) + ")"
    if self.current_rtt: 
      s += ": " "RTT [current (median/mean/dev)]: "
      s += str(self.current_rtt) + " (" + str(self.stats.median) + "/"
      s += str(self.stats.mean) + "/" + str(self.stats.dev) + ")"
    if self.rtt_created: s += "*"
    return s

class Stream(PathSupport.Stream):
  """ Stream class extended to hop """
  def __init__(self, sid, host, port, kind):
    PathSupport.Stream.__init__(self, sid, host, port, kind)
    self.hop = None	# save hop if this is a ping, hop=None is complete circ

## CircuitBuildingStats #######################################################

# TODO: Move to TorCtl.TorUtil?
class CircuitBuildingStats(Stats):
  """ Create an instance of this and gather overall circuit stats """
  def __init__(self):
    Stats.__init__(self)
    self.failures = 0   # count failures

  def to_string(self):
    """ Create a string for writing to a file """
    s = "Successful circuit buildups: "
    s += str(len(self.values)) + " records, median=" + str(self.median)
    s += " s, avg=" + str(self.mean) + " s" 
    s += ", dev=" + str(self.dev) + " s (min=" + str(self.min)
    s += " s, max=" + str(self.max) + " s)\n"
    s += "Circuits that failed during buildup: " + str(self.failures)
    return s

## NetworkModel ###############################################################

class LinkInfo:
  """ This class contains infos about a link: source, destination, RTT
      plus: rtt_history, methods to compute stats, etc. """
  def __init__(self, src, dest, rtt=0):
    # Set src and dest
    self.src = src
    self.dest = dest
    # The current value
    self.current_rtt = None
    # Set the RTT
    self.add_rtt(rtt)

  def add_rtt(self, rtt):
    # Compute new current value from the last
    if self.current_rtt == None: self.current_rtt = rtt
    else: 
      self.current_rtt = (self.current_rtt * 0.5) + (rtt * 0.5)
      plog("DEBUG", "Computing new current RTT from " + str(rtt) + " to " + 
         str(self.current_rtt))

class PathProposal:
  """ Instances of this class are path-proposals found in the model """
  def __init__(self, links, path):
    # This is a list of LinkInfo objects
    self.links = links
    # Also save the path for passing to build_circuit, cut off ROOT here
    self.path = path[1:len(path)]
    # Compute the expected RTT (from current value?)
    self.rtt = reduce(lambda x,y: x + y.current_rtt, self.links, 0.0)

  def to_string(self):
    """ Create a string for printing out information """
    s = ""
    for l in self.links:
      # Get the single objects
      s += l.src.nickname + "--" + l.dest.nickname +\
         " (" + str(l.current_rtt) + ") " + ", "
    return "Route proposal: " + s + "--> " + str(self.rtt) + " sec" 

class NetworkModel:  
  """ This class is used to record measured RTTs for single links in a model 
      of the 'currently explored subnet' (undirected graph) """  
  def __init__(self, rooter):
    """ Constructor: pass the root of all our circuits """
    # Use XDiGraph() (= directed)?
    self.graph = networkx.XGraph(name="Explored Tor Subnet", 
       selfloops=False, multiedges=False)
    # Initially add THIS proxy to the model
    self.root = rooter
    self.graph.add_node(self.root)
    self.proposals = []
    plog("DEBUG", "NetworkModel initiated: added " + self.root.nickname)

  def add_link(self, src, dest, rtt):
    """ Add a link to the graph given src, dest & rtt """
    self.graph.add_edge(src, dest, LinkInfo(src, dest, rtt))

  def add_circuit(self, c):
    """ Check if we can compute RTTs of single links for circuit c and store 
        these in the model """
    # Get the length
    path_len = len(c.path)
    # Go through the path
    for i in xrange(1,path_len):
      if i in c.part_rtts:
        # First hop --> add Link from Root to 1
        if i == 1:
	  link_rtt = c.part_rtts[i]
	  self.add_link(self.root, c.path[i-1], link_rtt)
	# Handle i -- (i+1)
        if i+1 in c.part_rtts:
          link_rtt = c.part_rtts[i+1] - c.part_rtts[i]
	  if link_rtt > 0:          
	    plog("INFO", "Computed link-RTT " + str(i) + ": " + str(link_rtt))
	    # Save to NetworkModel
	    self.add_link(c.path[i-1], c.path[i], link_rtt)
	  else:
	    plog("WARN", "Negative link-RTT " + str(i) + ": " + str(link_rtt))
	# Handle (n-1) -- n
	elif None in c.part_rtts:
          # We have a total value
	  link_rtt = c.part_rtts[None] - c.part_rtts[i]
	  if link_rtt > 0:          
	    plog("INFO", "Computed link-RTT " + str(i) + ": " + str(link_rtt))
	    # Save to NetworkModel
	    self.add_link(c.path[i-1], c.path[i], link_rtt)
	  else:
	    plog("WARN", "Negative link-RTT " + str(i) + ": " + str(link_rtt))

  def get_link_info(self, path):
    """ From a path given as list of routers, return link-infos """
    links = []
    for i in xrange(0, len(path)-1):
      # TODO: Check if edge exists
      links.append(self.graph.get_edge(path[i], path[i+1]))
    return links

  def find_circuits(self):
    # Reset list of proposals and prefixes for DFS
    self.proposals = []
    self.prefixes = {}
    # Measure for info
    start = time.time()
    # Start the search
    self.visit(self.root, [])
    # Sort proposals for their RTTs
    sort_list(self.proposals, lambda x: x.rtt)
    # Some logging
    plog("DEBUG", "Finding " + str(len(self.proposals)) + 
       " proposals and sorting them took us " + 
       str(time.time()-start) + " seconds")
    # Print all of them for debugging/info
    for p in self.proposals:
      print(p.to_string())

  def get_proposals(self, n):
    """ Return all proposals with rtt <= n seconds """
    ret = []
    for p in self.proposals:
      if p.rtt <= n:
	ret.append(p) 
    plog("DEBUG", "Found " + str(len(ret)) + 
       " path proposals having RTT <= " + str(n) + " sec")
    return ret

  def visit(self, node, path, i=1):
    """ Recursive Depth-First-Search: Maybe use some existing method? """
    if node not in path:
      path.append(node)
      # Root -- Exit
      if len(path) == 4:
        # We found a possible circuit: add to the proposals
        self.proposals.append(PathProposal(self.get_link_info(path), path))
      else:
        self.prefixes[i] = path
	# G is also a dict
        for n in self.graph[node]:
	  if n not in self.prefixes[i]:
	    self.visit(n, copy.copy(self.prefixes[i]), i+1)

  def print_graph(self):
    """ Print current info about the graph """
    print(self.graph.info())

## EventHandlers ##############################################################

# TODO: Move to PathSupport
class CircuitHandler(PathSupport.PathBuilder):
  """ CircuitHandler that extends from PathBuilder """
  def __init__(self, c, selmgr, num_circuits):
    # Init the PathBuilder
    PathSupport.PathBuilder.__init__(self, c, selmgr, GeoIPSupport.GeoIPRouter)
    self.num_circuits = num_circuits            # size of the circuit pool
    self.check_circuit_pool()	                # bring up the pool of circs
    self.circ_stats = CircuitBuildingStats()    # record setup-times
    # Filehandlers for saving stats about circuit building
    self.stats_logger = FileHandler("data/op-addon/circ-setup-stats")
    self.setup_logger = FileHandler("data/op-addon/circ-setup-durations")

  def check_circuit_pool(self):
    """ Init or check the status of our pool of circuits """
    # Get current number of circuits
    n = len(self.circuits.values())
    i = self.num_circuits - n
    if i > 0:
      plog("INFO", "Checked pool of circuits: we need to build " + 
         str(i) + " circuits")
    # Schedule (num_circs - n) circuit-buildups
    while (n < self.num_circuits):      
      self.build_idle_circuit()
      plog("DEBUG", "Scheduled circuit No. " + str(n+1))
      n += 1

  def close_circuit(self, id):
    """ Try to close a circuit with given id """
    # TODO: Pass streams to another circ before closing?
    self.circuits[id].closed = True
    try: self.c.close_circuit(id)
    except TorCtl.ErrorReply, e: 
      plog("ERROR", "Failed closing circuit " + str(id) + ": " + str(e))	    

  def print_circuits(self, list=None):
    """ Print out the circuits + some info, optionally pass a (sorted) list """
    if list: circs = list
    else: circs = self.circuits.values()
    plog("INFO", "We have " + str(len(circs)) + " circuits:")
    for c in circs:
      print("+ " + c.to_string())

  def build_idle_circuit(self):
    """ Build an idle circuit """
    circ = None
    while circ == None:
      try:
        # Configure which port to use here
	self.selmgr.set_target("255.255.255.255", 80)
        circ = self.c.build_circuit(self.selmgr.pathlen, 
           self.selmgr.path_selector)
	self.circuits[circ.circ_id] = circ
      except TorCtl.ErrorReply, e:
        # FIXME: How come some routers are non-existant? Shouldn't
        # we have gotten an NS event to notify us they disappeared?
        plog("NOTICE", "Error building circuit: " + str(e.args))
 
  def circ_status_event(self, c):
    """ Handle circuit status events """
    # Construct output for logging
    output = [c.event_name, str(c.circ_id), c.status]
    if c.path: output.append(",".join(c.path))
    if c.reason: output.append("REASON=" + c.reason)
    if c.remote_reason: output.append("REMOTE_REASON=" + c.remote_reason)
    plog("DEBUG", " ".join(output))
    
    # Circuits we don't control get built by Tor
    if c.circ_id not in self.circuits:
      plog("DEBUG", "Ignoring circuit " + str(c.circ_id) + 
         " (controlled by Tor or not yet in the list)")
      return
    
    # EXTENDED
    if c.status == "EXTENDED":
      # Compute elapsed time
      extend_time = c.arrived_at - self.circuits[c.circ_id].last_extended_at
      # Add to the list
      self.circuits[c.circ_id].extend_times.append(extend_time)
      plog("DEBUG", "Circuit " + str(c.circ_id) + " extended in " + 
         str(extend_time) + " sec")
      self.circuits[c.circ_id].last_extended_at = c.arrived_at
    
    # FAILED & CLOSED
    elif c.status == "FAILED" or c.status == "CLOSED":
      # XXX: Can still get a STREAM FAILED for this circ after this
      circ = self.circuits[c.circ_id]
      
      # Logging and statistics
      if not circ.built:
        message = ["FAILED"]
        if c.reason: message.append("REASON=" + c.reason)
        if c.remote_reason: message.append("REMOTE_REASON=" + c.remote_reason)
        self.setup_logger.append(" ".join(message) + ": " + 
           str(circ.extend_times))
        # Increase counter and write circ_stats to file
        self.circ_stats.failures += 1
        self.stats_logger.write(self.circ_stats.to_string()) 
      
      # Actual removal of the circ
      del self.circuits[c.circ_id]
      # Give away pending streams
      for stream in circ.pending_streams:
	plog("DEBUG", "Finding new circ for " + str(stream.strm_id))
        self.attach_stream_any(stream, stream.detached_from)
      # Check if there are enough circs
      self.check_circuit_pool()
      return
    
    # BUILT
    elif c.status == "BUILT":
      self.circuits[c.circ_id].built = True
      for stream in self.circuits[c.circ_id].pending_streams:
        try:
          self.c.attach_stream(stream.strm_id, c.circ_id)
        except TorCtl.ErrorReply, e:
          # No need to retry here. We should get the failed
          # event for either the circ or stream next
          plog("WARN", "Error attaching stream: " + str(e.args))
      
      # Log setup durations to file
      self.setup_logger.append(str(self.circuits[c.circ_id].extend_times))
      # Compute duration by summing up extend_times
      duration = reduce(lambda x, y: x+y, 
         self.circuits[c.circ_id].extend_times, 0.0)
      plog("DEBUG", "Circuit " + str(c.circ_id) + " needed " + 
         str(duration) + " seconds to be built")      
      # Add duration to circ_stats and write file
      self.circ_stats.add_value(duration)
      self.stats_logger.write(self.circ_stats.to_string())
      # Save the duration to the circuit for later use
      self.circuits[c.circ_id].setup_duration = duration
    
    # OTHER?
    else:
      # If this was e.g. a LAUNCHED
      pass

## StreamHandler ##############################################################

# TODO: Move to PathSupport
class StreamHandler(CircuitHandler):
  """ This is a StreamHandler that extends from the CircuitHandler """
  def __init__(self, c, selmgr, num_circs):    
    # Call constructor of superclass
    CircuitHandler.__init__(self, c, selmgr, num_circs)
    self.sorted_circs = None    # optional
    #self.new_nym = True

  def clear_dns_cache(self):
    """ Send signal CLEARDNSCACHE """
    lines = self.c.sendAndRecv("SIGNAL CLEARDNSCACHE\r\n")
    for _, msg, more in lines:
      plog("DEBUG", "CLEARDNSCACHE: " + msg)

  def close_stream(self, id, reason):
    """ Close a stream with given id and reason """
    self.c.close_stream(id, reason)

  def create_and_attach(self, stream, unattached_streams):
    """ Create a new circuit and attach (stream + unattached_streams) """
    circ = None
    self.selmgr.set_target(stream.host, stream.port)
    while circ == None:
      try:
        circ = self.c.build_circuit(self.selmgr.pathlen, self.selmgr.path_selector)
      except TorCtl.ErrorReply, e:
        plog("NOTICE", "Error building circ: " + str(e.args))
    for u in unattached_streams:
      plog("DEBUG", "Attaching " + str(u.strm_id) + " pending build of circuit " + str(circ.circ_id))
      u.pending_circ = circ      
    circ.pending_streams.extend(unattached_streams)
    self.circuits[circ.circ_id] = circ
    self.last_exit = circ.exit
 
  def attach_stream_any(self, stream, badcircs):
    """ Attach a regular user stream """
    unattached_streams = [stream]
    if self.new_nym:
      self.new_nym = False
      plog("DEBUG", "Obeying new nym")
      for key in self.circuits.keys():
        if (not self.circuits[key].dirty
            and len(self.circuits[key].pending_streams)):
          plog("WARN", "New nym called, destroying circuit "+str(key)
             +" with "+str(len(self.circuits[key].pending_streams))
             +" pending streams")
          unattached_streams.extend(self.circuits[key].pending_streams)
          del self.circuits[key].pending_streams[:]
        # FIXME: Consider actually closing circs if no streams
        self.circuits[key].dirty = True

    # Check if there is a sorted list of circs
    if self.sorted_circs: list = self.sorted_circs
    else: list = self.circuits.values()
    for circ in list:
      # Check each circuit
      if circ.built and not circ.closed and circ.circ_id not in badcircs and not circ.dirty:
        if circ.exit.will_exit_to(stream.host, stream.port):
          try:
            self.c.attach_stream(stream.strm_id, circ.circ_id)
            stream.pending_circ = circ # Only one possible here
            circ.pending_streams.append(stream)    
            # Clear cache after the attach?
	    #self.clear_dns_cache()
            self.last_exit = circ.exit
          except TorCtl.ErrorReply, e:
            # No need to retry here. We should get the failed
            # event for either the circ or stream next
            plog("WARN", "Error attaching stream: " + str(e.args))
            return
          break
	else:
	  plog("DEBUG", "Circuit " + str(circ.circ_id) + " won't exit")
    else:
      self.create_and_attach(stream, unattached_streams)

  def stream_status_event(self, s):
    """ Catch user stream events """
    # Construct debugging output
    output = [s.event_name, str(s.strm_id), s.status, str(s.circ_id), s.target_host, str(s.target_port)]
    if s.reason: output.append("REASON=" + s.reason)
    if s.remote_reason: output.append("REMOTE_REASON=" + s.remote_reason)
    plog("DEBUG", " ".join(output))
     
    # If target_host is not an IP-address
    if not re.match(r"\d+.\d+.\d+.\d+", s.target_host):
      s.target_host = "255.255.255.255" # ignore DNS for exit policy check
    
    # NEW or NEWRESOLVE
    if s.status == "NEW" or s.status == "NEWRESOLVE":
      if s.status == "NEWRESOLVE" and not s.target_port:
        s.target_port = self.resolve_port      
      # Set up the new stream
      stream = Stream(s.strm_id, s.target_host, s.target_port, s.status)
      self.streams[s.strm_id] = stream        
      self.attach_stream_any(self.streams[s.strm_id], self.streams[s.strm_id].detached_from)
    
    # DETACHED
    elif s.status == "DETACHED":
      # Stream not found
      if s.strm_id not in self.streams:
        plog("WARN", "Detached stream " + str(s.strm_id) + " not found")
        self.streams[s.strm_id] = Stream(s.strm_id, s.target_host, s.target_port, "NEW")
      # Circuit not found
      if not s.circ_id:
        plog("WARN", "Stream " + str(s.strm_id) + " detached from no circuit!")
      else:
        self.streams[s.strm_id].detached_from.append(s.circ_id)      
      # Detect timeouts on user streams
      if s.reason == "TIMEOUT":
	# TODO: Count timeouts on the stream?
	#self.streams[s.strm_id].timeout_counter += 1
	plog("DEBUG", "User stream timed out on circuit " + str(s.circ_id))
      # Stream was pending
      if self.streams[s.strm_id] in self.streams[s.strm_id].pending_circ.pending_streams:
        self.streams[s.strm_id].pending_circ.pending_streams.remove(self.streams[s.strm_id])
      # Attach to another circ
      self.streams[s.strm_id].pending_circ = None
      self.attach_stream_any(self.streams[s.strm_id], self.streams[s.strm_id].detached_from)

    # SUCCEEDED
    if s.status == "SUCCEEDED":
      if s.strm_id not in self.streams:
        plog("NOTICE", "Succeeded stream " + str(s.strm_id) + " not found")
        return
      if s.circ_id and self.streams[s.strm_id].pending_circ.circ_id != s.circ_id:
        # Hrmm.. this can happen on a new-nym.. Very rare, putting warn
        # in because I'm still not sure this is correct
        plog("WARN", "Mismatch of pending: "
          + str(self.streams[s.strm_id].pending_circ.circ_id) + " vs "
          + str(s.circ_id))
	self.streams[s.strm_id].circ = self.circuits[s.circ_id]
      else:
        self.streams[s.strm_id].circ = self.streams[s.strm_id].pending_circ
      self.streams[s.strm_id].pending_circ.pending_streams.remove(self.streams[s.strm_id])
      self.streams[s.strm_id].pending_circ = None
      self.streams[s.strm_id].attached_at = s.arrived_at

    # FAILED or CLOSED
    elif s.status == "FAILED" or s.status == "CLOSED":
      if s.strm_id not in self.streams:
        plog("NOTICE", "Failed stream " + str(s.strm_id) + " not found")
        return
      #if not s.circ_id: plog("WARN", "Stream " + str(s.strm_id) + " closed/failed from no circuit")
      # We get failed and closed for each stream. OK to return and let the CLOSED do the cleanup
      if s.status == "FAILED":
        # Avoid busted circuits that will not resolve or carry traffic
        self.streams[s.strm_id].failed = True
	if s.circ_id in self.circuits: self.circuits[s.circ_id].dirty = True
        elif self.streams[s.strm_id].attached_at != 0: 
	  plog("WARN", "Failed stream on unknown circuit " + str(s.circ_id))
	return
      # CLOSED
      if self.streams[s.strm_id].pending_circ:
        self.streams[s.strm_id].pending_circ.pending_streams.remove(self.streams[s.strm_id])
      # Actual removal of the stream
      del self.streams[s.strm_id]

    # REMAP
    elif s.status == "REMAP":
      if s.strm_id not in self.streams:
        plog("WARN", "Remap id "+str(s.strm_id)+" not found")
      else:
        if not re.match(r"\d+.\d+.\d+.\d+", s.target_host):
          s.target_host = "255.255.255.255"
          plog("NOTICE", "Non-IP remap for "+str(s.strm_id) + " to " + s.target_host)		   
        self.streams[s.strm_id].host = s.target_host
        self.streams[s.strm_id].port = s.target_port

## PingHandler ################################################################

class PingHandler(StreamHandler):
  """ This class extends the general StreamHandler to handle ping-requests """
  def __init__(self, c, selmgr, num_circs, router, partial=False):
    # Anything ping-related
    self.ping_queue = Queue.Queue()	# (circ_id, hop)-pairs
    self.start_times = {}		# dict mapping (circ_id, hop):start_time
    # Additional stuff for measuring single links
    self.partial_circs = partial
    if self.partial_circs:
      self.router = router			# object that represents this OR
      self.model = NetworkModel(self.router)	# model for recording link-RTTs
    # Handle testing_mode
    if testing_mode:
      self.latency_logger= FileHandler("data/op-addon/mean-latencies")
    # Init the StreamHandler
    StreamHandler.__init__(self, c, selmgr, num_circs)
    # Sorted circuit list
    self.sorted_circs = []		# list of circs sorted by current RTT
    # Start the Pinger that triggers the connections
    self.pinger = Pinger(self)
    self.pinger.setDaemon(True)
    self.pinger.start()

  def refresh_sorted_list(self):
    """ Sort the list for their current RTTs """
    def notlambda(x): 
      # If not measured yet, return a max value
      if x.current_rtt == None: return 10
      else: return x.current_rtt
    self.sorted_circs = sort_list(self.circuits.values(), notlambda)
    plog("DEBUG", "Refreshed sorted list of circuits")

  def enqueue_pings(self):
    """ To be schedule_immediated by pinger before the initial connection is triggered """
    print("")
    self.refresh_sorted_list()
    # XXX: Check if there are any, else let the Pinger wait a bit?
    circs = self.circuits.values()
    for c in circs:
      if c.built:
        # Get id of c
      	id = c.circ_id
        if self.partial_circs:
	  # If partial measurings wanted: get length
	  path_len = len(c.path)
	  for i in xrange(1, path_len):
            self.ping_queue.put((id, i))
            plog("DEBUG", "Enqueued circuit " + str(id) + " hop " + str(i))
	# And for the whole circuit ...
        self.ping_queue.put((id, None))
        plog("DEBUG", "Enqueued circuit " + str(id) + " hop None")

  def established(self, circ_list):
    """ Check if there is at least one circuit built """
    for c in circ_list:
      if c.built:
        return True

  def attach_ping(self, stream):
    """ Attach a ping stream to its circuit """
    if self.ping_queue.empty():
      # This round has finished
      plog("INFO", "Queue is empty --> round has finished, closing stream " 
         + str(stream.strm_id))
      self.close_stream(stream.strm_id, 5)
      # Clear start_times
      self.start_times.clear()
      # Call the rest from here?
      self.print_circuits(self.sorted_circs)
      if self.partial_circs:
        # Print out the model
        self.model.print_graph()
        self.model.find_circuits()
      # Enqueue again all circs
      self.enqueue_pings()

    else:
      # Get the info and extract
      ping_info = self.ping_queue.get()
      circ_id = ping_info[0]
      hop = ping_info[1]
      # Set circ to stream
      stream.circ = circ_id
      try:
        # Get the circuit 
        if circ_id in self.circuits:
          circ = self.circuits[circ_id]
          if circ.built and not circ.closed:        
            stream.hop = hop
	    self.c.attach_stream(stream.strm_id, circ.circ_id, hop)
            # Don't use pending for pings
          else:
            plog("WARN", "Circuit not built or closed")
	    self.attach_ping(stream)
        else:
          # Go to next test if circuit is gone or we get an ErrorReply
          plog("WARN", "Circuit " + str(circ_id) + 
             " does not exist anymore --> passing")
          self.attach_ping(stream)
      except TorCtl.ErrorReply, e:
        plog("WARN", "Error attaching stream " + str(stream.strm_id) + 
           " :" + str(e.args))
	self.attach_ping(stream)

  def log_circuit(self, circ):
    """ To be called when num_tests is reached for writing 
        any interesting values to a file before closing circ """
    self.latency_logger.append(str(circ.stats.median) + "\t" + 
       str(circ.stats.mean) + "\t" + str(circ.setup_duration))
    line_count = self.latency_logger.get_line_count()
    if line_count >= num_records:
      plog("INFO", "Enough records, exiting. (line_count = " + 
         str(line_count) + ")")
      # XXX: How to kill the parent thread from here?
      sys.exit(1)

  def record_ping(self, s):
    """ Record a ping from a stream event (DETACHED or CLOSED) """
    # No timeout, this is a successful ping: measure here	  
    hop = self.streams[s.strm_id].hop
    # Compute RTT using arrived_at 
    rtt = s.arrived_at - self.start_times[(s.circ_id, hop)]
    plog("INFO", "Measured RTT: " + str(rtt) + " sec")
    # Save RTT to circuit
    self.circuits[s.circ_id].part_rtts[hop] = rtt
    
    if hop == None:
      # This is a total circuit measuring
      self.circuits[s.circ_id].add_rtt(rtt)
      plog("DEBUG", "Added RTT to history: " + 
         str(self.circuits[s.circ_id].stats.values))	  
      
      # TESTING_MODE: close if num_tests is reached  
      if testing_mode:
        if self.circuits[s.circ_id].age == num_tests:
          plog("DEBUG", "Closing circ " + str(s.circ_id) + 
             ": num_tests is reached")
          # Save stats to a file for generating plots etc.
          if self.partial_circs:
	    if self.circuits[s.circ_id].rtt_created:
	      # TODO: Do we want to check if this circuit is *really* new?
              self.log_circuit(self.circuits[s.circ_id])
          else:
            self.log_circuit(self.circuits[s.circ_id])
          # Close the circuit
          self.close_circuit(s.circ_id)
      
      # Close if slow-max is reached on current RTTs
      if self.circuits[s.circ_id].current_rtt >= slow:
        self.circuits[s.circ_id].slowness_counter += 1
        if slowness_limit > 0:
          if self.circuits[s.circ_id].slowness_counter >= slowness_limit: 
            if not self.circuits[s.circ_id].closed:
              plog("DEBUG", "Slow-max (" + str(slowness_limit) + 
                 ") is reached --> closing circuit " + str(s.circ_id))
              self.close_circuit(s.circ_id)
      # Resort only if this is for the complete circ
      self.refresh_sorted_list()
      if self.partial_circs == True:
        # Add the links of this circuit to the model
        self.model.add_circuit(self.circuits[s.circ_id])

  def stream_status_event(self, s):
    """ Separate pings from regular streams directly """
    if not (s.target_host == ping_dummy_host and s.target_port == ping_dummy_port):
      # This is no ping, call the other method
      return StreamHandler.stream_status_event(self, s)
    
    # Construct debugging output
    output = [s.event_name, str(s.strm_id), s.status, str(s.circ_id), s.target_host, str(s.target_port)]
    if s.reason: output.append("REASON=" + s.reason)
    if s.remote_reason: output.append("REMOTE_REASON=" + s.remote_reason)
    plog("DEBUG", " ".join(output))
 
    # NEW or NEWRESOLVE
    if s.status == "NEW":
      # Set up the stream object
      stream = Stream(s.strm_id, s.target_host, s.target_port, s.status)
      self.streams[s.strm_id] = stream        
      self.attach_ping(stream)

    # SENTCONNECT 
    elif s.status == "SENTCONNECT":
      # Measure here, means save arrived_at in the dict
      self.start_times[(s.circ_id, self.streams[s.strm_id].hop)] = s.arrived_at
  
    # DETACHED
    elif s.status == "DETACHED":      
      if (s.reason == "TIMEOUT"):
        self.circuits[s.circ_id].timeout_counter += 1
        self.circuits[s.circ_id].slowness_counter += 1
        plog("DEBUG", str(self.circuits[s.circ_id].timeout_counter) + " timeout(s) on circuit " + str(s.circ_id))
        if timeout_limit > 0:
          if self.circuits[s.circ_id].timeout_counter >= timeout_limit and not self.circuits[s.circ_id].closed:
            # Close the circuit
            plog("DEBUG", "Reached limit on timeouts --> closing circuit " + str(s.circ_id))
            self.close_circuit(s.circ_id)
        # Set RTT for this circ to None
        self.circuits[s.circ_id].current_rtt = None
      else:
        # No timeout: Record the result
        self.record_ping(s)              
      # Close the stream
      self.close_stream(s.strm_id, 5)

    # CLOSED + END is also ping, some routers send it when measuring 1-hop
    # better measure on FAILED?
    elif s.status == "CLOSED":
      if s.reason == "END":
        # Only record
        self.record_ping(s)

  def get_trad_circs(self):
    """ Count the circuits with rtt_created == False """
    trad_circs = 0
    for c in self.circuits.values():
      if c.rtt_created == False:
        trad_circs += 1
    return trad_circs

  def path_is_ok(self, path):
    """ Check if we currently do not have (TODO: had?) a circuit 
        with the given path (= Routers) """
    for c in self.circuits.values():
      if c.path == path: return False
    # XXX: Check if this path can exit?
    if not path[len(path)-1].will_exit_to("255.255.255.255", 80): 
      plog("ERROR", "Proposed circuit would not exit")
      return False
    return True

  def build_idle_circuit(self):
    """ Override from CircuitHandler to support circuit-creation from the NetworkModel """
    if self.partial_circs:
      circ = None
      # This is to ensure expansion of the explored subnet
      # Check if ratio would be ok when adding new rtt_created circ
      trad = float(self.get_trad_circs())
      ratio = trad/(len(self.circuits.values())+1)
      plog("DEBUG","Expected Ratio = " + str(ratio) + " >= " + str(min_ratio) + " ?")
      if ratio >= min_ratio:
        # Get the proposals RTT <= slow
	proposals = self.model.get_proposals(slow)
	# Check if we have >= min_proposals
        if len(proposals) >= min_proposals:
	  proposals = sort_list(proposals, lambda x: x.rtt)
	  # Check them out
	  while len(proposals) >= 1:
	    # Random choice or choose the fastest!
	      
	    choice = random.choice(proposals)
            #choice = proposals[0]
            # TODO: Probabilistic selection
            	    
            # Check if we already have a circ with this path
            if self.path_is_ok(choice.path):
              plog("INFO", "Chosen proposal: " + choice.to_string())
              try:
                circ = self.c.build_circuit_from_path(choice.path)
                circ.rtt_created = True
                self.circuits[circ.circ_id] = circ
	        return
              except TorCtl.ErrorReply, e:
                plog("NOTICE", "Error building circuit: " + str(e.args))
            else:
              # Remove this proposals
              plog("DEBUG", "Proposed circuit already exists")
	      proposals.remove(choice)
    
    # Build a circuit with the standard method
    plog("DEBUG", "Falling back to normal path selection")
    CircuitHandler.build_idle_circuit(self)
        
## Pinger #####################################################################

class Pinger(threading.Thread):
  """ Separate thread that triggers the Socks4-connections for pings """
  def __init__(self, ping_handler):
    self.handler = ping_handler		# the PingHandler
    threading.Thread.__init__(self)	# call the thread-constructor
  
  def run(self):
    """ The run()-method """
    time.sleep(initial_interval)
    self.handler.schedule_immediate(lambda x: x.enqueue_pings())
    while self.isAlive():
      self.ping()
      time.sleep(sleep_interval)
  
  # No "try .. except .. finally .." in Python < 2.5 !
  def ping(self):
    """ Create a connection to dummy_host/_port using Socks4 """
    s = None
    try:
      try:
        s = socks.socksocket()
        s.setproxy(socks.PROXY_TYPE_SOCKS4, socks_host, socks_port)
        s.connect((ping_dummy_host, ping_dummy_port))
      except socks.Socks4Error, e:
	# Don't do nothing, this will actually happen
	# print("Got Exception: " + str(e))
	pass
    finally:
      # Close the socket if open
      if s: s.close()

## End of Classes #############################################################

def connect(host, port):
  """ Return a connection to Tor's control port """
  sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  sock.connect((host, port))
  return Connection(sock)
 
def setup_location(conn):
  """ Setup a router object representing this proxy """
  global path_config
  ip = None
  try:
    # Try to get our IP
    info = conn.get_info("address")
    ip = info["address"]
  except: 
    plog("ERROR", "Could not get our IP")
    ip = "127.0.0.1"
  # Set up a router object
  router = GeoIPSupport.GeoIPRouter(TorCtl.Router(None,"ROOT",None,False,None,None,ip,None,None))
  # TODO: Check if ip == None?
  plog("INFO", "Our IP address is " + router.get_ip_dotted() + " [" + str(router.country_code) + "]")
  # Set entry_country here?
  # path_config.entry_country = router.country_code
  return router
 
def configure(conn):
  """ Set events and options """
  conn.set_events([TorCtl.EVENT_TYPE.STREAM,
      TorCtl.EVENT_TYPE.CIRC,
      TorCtl.EVENT_TYPE.NS,	  
      TorCtl.EVENT_TYPE.NEWDESC], True)
  # Set options: We attach streams now & build circuits
  conn.set_option("__LeaveStreamsUnattached", "1")
  conn.set_option("__DisablePredictedCircuits", "1")

def startup(argv):
  try:
    # Connect to Tor process
    conn = connect(config.get(HOST_PORT, "control_host"),
       config.getint(HOST_PORT, "control_port"))
    conn.authenticate()
    #conn.debug(file("control.log", "w"))
  except socket.error, e:
    plog("ERROR", "Could not connect to Tor process .. running?")
    return
  # Setup a router instance here
  router = setup_location(conn)
  # Configure myself  
  configure(conn)
  # Get the size of the circuit-pool from config
  num_circs = config.getint(CIRC_MANAGEMENT, "idle_circuits")
  # Set an EventHandler to the connection
  if measure_circs:
    # We measure latencies
    if measure_partial_circs:
      handler = PingHandler(conn, __selmgr, num_circs, router, True)
    else:
      handler = PingHandler(conn, __selmgr, num_circs, router)
  else:
    # No pings, only a StreamHandler
    handler = StreamHandler(conn, __selmgr, num_circs)
  conn.set_event_handler(handler)
  # Go to sleep to be able to get killed from the commandline
  # TODO: Do this only if not in testing_mode?
  try:
    while True:
      time.sleep(60)
  except KeyboardInterrupt:
    cleanup(conn)
    sys.exit(1)

def cleanup(conn):
  """ To be called on exit """
  plog("INFO", "Cleaning up...")
  conn.set_option("__LeaveStreamsUnattached", "0")
  conn.set_option("__DisablePredictedCircuits", "0")
  conn.close()

if __name__ == '__main__':
  plog("INFO", "This is OP-Addon v" + VERSION)
  startup(sys.argv)
