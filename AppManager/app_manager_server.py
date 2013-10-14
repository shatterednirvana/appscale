""" This service starts and stops application servers of a given application.
"""
# Programmer: Navraj Chohan <nlake44@gmail.com>

import glob
import json
import logging
import os
import random
import SOAPpy
import socket
import subprocess
import sys
import time
import urllib

from M2Crypto import SSL


sys.path.append(os.path.join(os.path.dirname(__file__), "../lib/"))
import appscale_info
import constants
import file_io
import monit_app_configuration
import monit_interface 
import misc 

# Most attempts to see if an application server comes up before failing
MAX_FETCH_ATTEMPTS = 7

# The initial amount of time to wait when trying to check if an application
# is up or not. In seconds.
INITIAL_BACKOFF_TIME = 1

# The PID number to return when a process did not start correctly
BAD_PID = -1

# Required configuration fields for starting an application
REQUIRED_CONFIG_FIELDS = ['app_name', 
                          'app_port', 
                          'language', 
                          'load_balancer_ip', 
                          'load_balancer_port', 
                          'xmpp_ip',
                          'dblocations',
                          'env_vars']

# The web path to fetch to see if the application is up
FETCH_PATH = '/_ah/health_check'

# Apps which can access any application's data.
TRUSTED_APPS = ["appscaledashboard"]

# The flag to tell the application server that this application can access
# all application data.
TRUSTED_FLAG = "--trusted"

# The location on the filesystem where the PHP executable is installed.
PHP_CGI_LOCATION = "/usr/local/php-5.4.15/installdir/bin/php-cgi"

def convert_config_from_json(config):
  """ Takes the configuration in JSON format and converts it to a dictionary.
      Validates the dictionary configuration before returning.
  
  Args:
    config: The configuration to convert
  Returns:
    None if it failed to convert the config and a dictionary if it succeeded
  """
  logging.info("Configuration for app:" + str(config))
  try:
    config = json.loads(config)
  except ValueError, e:
    logging.error("%s Exception--Unable to parse configuration: %s"%\
                   (e.__class__, str(e)))
    return None
  except TypeError, e:
    logging.error("%s Exception--Unable to parse configuration: %s"%\
                   (e.__class__, str(e)))
    return None

  if is_config_valid(config): 
    return config
  else:
    return None
  
def start_app(config):
  """ Starts a Google App Engine application on this machine. It 
      will start it up and then proceed to fetch the main page.
  
  Args:
    config: a dictionary that contains 
       app_name: Name of the application to start
       app_port: Port to start on 
       language: What language the app is written in
       load_balancer_ip: Public ip of load balancer
       load_balancer_port: Port of load balancer
       xmpp_ip: IP of XMPP service 
       dblocations: List of database locations 
       env_vars: A dict of environment variables that should be passed to the
        app.
  Returns:
    PID of process on success, -1 otherwise
  """
  config = convert_config_from_json(config)
  if config == None:
    logging.error("Invalid configuration for application")
    return BAD_PID 
  
  if not misc.is_app_name_valid(config['app_name']):
    logging.error("Invalid app name for application: " +\
                  config['app_name'])
    return BAD_PID
  logging.info("Starting %s application %s"%(config['language'], 
                                             config['app_name']))

  start_cmd = ""
  stop_cmd = ""
  env_vars = config['env_vars']
  watch = "app___" + config['app_name']
 
  if config['language'] == constants.PYTHON27 or \
       config['language'] == constants.GO or \
       config['language'] == constants.PHP:
    start_cmd = create_python27_start_cmd(config['app_name'],
                            config['load_balancer_ip'],
                            config['app_port'],
                            config['load_balancer_ip'],
                            config['load_balancer_port'],
                            config['xmpp_ip'],
                            config['dblocations'],
                            config['language'])
    logging.info(start_cmd)
    stop_cmd = create_python27_stop_cmd(config['app_port'], config['language'])
    env_vars.update(create_python_app_env(config['load_balancer_ip'],
                            config['load_balancer_port'], 
                            config['app_name']))
  elif config['language'] == constants.JAVA:
    copy_successful = copy_modified_jars(config['app_name'])
    if not copy_successful:
      return BAD_PID
    start_cmd = create_java_start_cmd(config['app_name'],
                            config['app_port'],
                            config['load_balancer_ip'],
                            config['load_balancer_port'],
                            config['dblocations'])
    stop_cmd = create_java_stop_cmd(config['app_port'])
    env_vars.update(create_java_app_env())
  else:
    logging.error("Unknown application language %s for appname %s"\
                  %(config['language'], config['app_name'])) 
    return BAD_PID

  logging.info("Start command: " + str(start_cmd))
  logging.info("Stop command: " + str(stop_cmd))
  logging.info("Environment variables: " +str(env_vars))

  monit_app_configuration.create_config_file(str(watch),
                                             str(start_cmd), 
                                             str(stop_cmd), 
                                             [config['app_port']],
                                             env_vars)

  if not monit_interface.start(watch):
    logging.error("Unable to start application server with monit")
    return BAD_PID

  if not wait_on_app(int(config['app_port'])):
    logging.error("Application server did not come up in time, " + \
                   "removing monit watch")
    monit_interface.stop(watch)
    return BAD_PID

  return 0

def stop_app_instance(app_name, port):
  """ Stops a Google App Engine application process instance on current 
      machine.

  Args:
    app_name: Name of application to stop
    port: The port the application is running on
  Returns:
    True on success, False otherwise
  """
  if not misc.is_app_name_valid(app_name):
    logging.error("Unable to kill app process %s on port %d because of " +\
                  "invalid name for application"%(app_name, int(port)))
    return False

  logging.info("Stopping application %s"%app_name)
  watch = "app___" + app_name + "-" + str(port)
  return monit_interface.stop(watch, is_group=False)

def restart_app_instances_for_app(app_name):
  """ Restarts all instances of a Google App Engine application on this machine.

  Args:
    app_name: The application ID corresponding to the app to restart.

  Returns:
    True if successful, and False otherwise.
  """
  if not misc.is_app_name_valid(app_name):
    logging.error("Unable to kill app process %s on because of " +\
                  "invalid name for application"%(app_name))
    return False

  logging.info("Restarting application %s"%app_name)
  watch = "app___" + app_name
  return monit_interface.restart(watch)
 
def stop_app(app_name):
  """ Stops all process instances of a Google App Engine application on this 
      machine.

  Args:
    app_name: Name of application to stop
  Returns:
    True on success, False otherwise
  """
  if not misc.is_app_name_valid(app_name):
    logging.error("Unable to kill app process %s on because of " +\
                  "invalid name for application"%(app_name))
    return False

  logging.info("Stopping application %s"%app_name)
  watch = "app___" + app_name
  monit_result = monit_interface.stop(watch)
 
  if not monit_result:
    logging.error("Unable to shut down monit interface for watch %s"%watch)
    return False

  return True

############################################
# Private Functions (but public for testing)
############################################
def wait_on_app(port):
  """ Waits for the application hosted on this machine, on the given port, 
      to respond to HTTP requests.
  
  Args:
    port: Port where app is hosted on the local machine
  Returns:
    True on success, False otherwise
  """
  backoff = INITIAL_BACKOFF_TIME
  retries = MAX_FETCH_ATTEMPTS
  private_ip = appscale_info.get_private_ip()

  url = "http://" + private_ip + ":" + str(port) + FETCH_PATH
  while retries > 0:
    try:
      urllib.urlopen(url)
      return True
    except IOError:
      retries -= 1

    logging.warning("Application was not up at %s, retrying in %d seconds"%\
                   (url, backoff))
    time.sleep(backoff)
    backoff *= 2

  logging.error("Application did not come up on %s after %d attemps"%\
                (url, MAX_FETCH_ATTEMPTS))
  return False
     
def choose_db_location(db_locations):
  """ Given a string containing multiple datastore locations
      select one randomly to spread load.

  Args:
    db_locations: A list of datastore locations
  Returns:
    An IP address that can be used for datastore access
  Raise:
    ValueError: if there are no locations given in the args.
  """
  if len(db_locations) == 0: 
    raise ValueError("DB locations " + \
                     "were not correctly set: " + str(db_locations))

  index = random.randint(0, len(db_locations) - 1)
  return db_locations[index]

def create_python_app_env(public_ip, port, app_name):
  """ Returns the environment variables the python application server uses.
  
  Args:
    public_ip: The public IP of the load balancer
    port: The port the application is using
    app_name: The name of the application to be run
  Returns:
    A dictionary containing the environment variables
  """
  env_vars = {}
  env_vars['MY_IP_ADDRESS'] = public_ip
  env_vars['MY_PORT'] = str(port)
  env_vars['APPNAME'] = app_name
  env_vars['GOMAXPROCS'] = appscale_info.get_num_cpus()
  env_vars['APPSCALE_HOME'] = constants.APPSCALE_HOME
  return env_vars

def create_java_app_env():
  """ Returns the environment variables java application servers uses.
  
  Returns:
    A dictionary containing the environment variables  
  """
  env_vars = {}
  env_vars['APPSCALE_HOME'] = constants.APPSCALE_HOME
  return env_vars

def create_python27_start_cmd(app_name,
                              login_ip, 
                              port, 
                              load_balancer_host, 
                              load_balancer_port,
                              xmpp_ip,
                              db_locations,
                              py_version):
  """ Creates the start command to run the python application server.
  
  Args:
    app_name: The name of the application to run
    login_ip: The public IP
    port: The local port the application server will bind to
    load_balancer_host: The host of the load balancer
    load_balancer_port: The port of the load balancer
    xmpp_ip: The IP of the XMPP service
    py_version: The version of python to use
  Returns:
    A string of the start command.
  """
  db_location = choose_db_location(db_locations)
  cmd = ["python",
         constants.APPSCALE_HOME + "/AppServer/dev_appserver.py",
         "--port " + str(port),
         "--admin_port " + str(port + 10000),
         "--cookie_secret " + appscale_info.get_secret(),
         "--login_server " + login_ip,
         "--skip_sdk_update_check",
         "--nginx_host " + str(load_balancer_host),
         "--require_indexes",
         "--enable_sendmail",
         "--xmpp_path " + xmpp_ip,
         "--php_executable_path=" + str(PHP_CGI_LOCATION),
         "--uaserver_path " + db_location + ":"\
               + str(constants.UA_SERVER_PORT),
         "--datastore_path " + db_location + ":"\
               + str(constants.DB_SERVER_PORT),
         "/var/apps/" + app_name + "/app",
         "--host " + appscale_info.get_private_ip()]
 
  if app_name in TRUSTED_APPS:
    cmd.extend([TRUSTED_FLAG])
 
  return ' '.join(cmd)

def copy_modified_jars(app_name):
  """
  Copies the changes made to the Java SDK
  for AppScale into the apps lib folder.

  Args: 
    app_name: The name of the application to run

  Returns:
    False if there were any errors, True if success
  """
  appscale_home = constants.APPSCALE_HOME

  cp_result = subprocess.call("cp " +  appscale_home + "/AppServer_Java/" +\
                              "appengine-java-sdk-repacked/lib/user/*.jar " +\
                              "/var/apps/" + app_name + "/app/war/WEB-INF/" +\
                              "lib/", shell=True)
  if cp_result != 0:
    logging.error("Failed to copy appengine-java-sdk-repacked/lib/user jars " +\
                  "to lib directory of " + app_name)
    return False
  
  cp_result = subprocess.call("cp " + appscale_home + "/AppServer_Java/" +\
                              "appengine-java-sdk-repacked/lib/impl/" +\
                              "appscale-*.jar /var/apps/" + app_name + "/" +\
                              "app/war/WEB-INF/lib/", shell=True)

  if cp_result != 0:
    logging.error("Failed to copy email jars to lib directory of " + app_name)
    return False

  return True

def create_java_start_cmd(app_name,
                          port, 
                          load_balancer_host, 
                          load_balancer_port,
                          db_locations):
  """
  Creates the start command to run the java application server.
  
  Args:
    app_name: The name of the application to run
    port: The local port the application server will bind to
    load_balancer_host: The host of the load balancer
    load_balancer_port: The port of the load balancer
    xmpp_ip: The IP of the XMPP service
  Returns:
    A string of the start command.
  """
  db_location = choose_db_location(db_locations)

  cmd = ["cd " + constants.JAVA_APPSERVER + " &&",
             "./genKeystore.sh &&",
             "./appengine-java-sdk-repacked/bin/dev_appserver.sh",
             "--port=" + str(port),
             #this jvm flag allows javax.email to connect to the smtp server
             "--jvm_flag=-Dsocket.permit_connect=true",
             "--disable_update_check",
             "--cookie_secret=" + appscale_info.get_secret(),
             "--address=" + appscale_info.get_private_ip(),
             "--datastore_path=" + db_location,
             "--login_server=" + load_balancer_host,
             "--appscale_version=1",
             "--NGINX_ADDRESS=" + load_balancer_host,
             "--NGINX_PORT=" + str(load_balancer_port),
             "/var/apps/" + app_name +"/app/war/",
             ]

  return ' '.join(cmd)

def choose_python_executable(py_version):
  """ Selects the correct executable of python to use.

  Args:
    py_version: A string of the python version
  Returns:
    String of python executable path
  """
  return "/usr/bin/python"

def create_python27_stop_cmd(port, py_version):
  """ This creates the stop command for an application which is 
  uniquely identified by a port number. Additional portions of the 
  start command are included to prevent the termination of other 
  processes. 
  
  Args: 
    port: The port which the application server is running
    py_version: The python version the app is currently using
  Returns:
    A string of the stop command.
  """
  stop_cmd = "/usr/bin/python /root/appscale/stop_service.py " \
    "dev_appserver.py {0}".format(port)
  return stop_cmd

def create_java_stop_cmd(port):
  """ This creates the stop command for an application which is 
  uniquely identified by a port number. Additional portions of the 
  start command are included to prevent the termination of other 
  processes. 
  
  Args: 
    port: The port which the application server is running
  Returns:
    A string of the stop command.
  """
  stop_cmd = "/usr/bin/python /root/appscale/stop_service.py " \
    "java {0}".format(port)
  return stop_cmd

def is_config_valid(config):
  """ Takes a configuration and checks to make sure all required properties 
    are present.
 
  Args:
    config: The dictionary to validate
  Returns:
    True if valid, False otherwise
  """
  for ii in REQUIRED_CONFIG_FIELDS:
    try:
      if config[ii]:
        pass
    except KeyError:
      logging.error("Unable to find " + str(ii) + " in configuration")
      return False 
  return True

def usage():
  """ Prints usage of this program """
  print "args: --help or -h for this menu"

################################
# MAIN
################################
if __name__ == "__main__":
  for ii in range(1, len(sys.argv)):
    if sys.argv[ii] in ("-h", "--help"):
      usage()
      sys.exit()
  
  internal_ip = socket.gethostbyname(socket.gethostname())
  server = SOAPpy.SOAPServer((internal_ip, constants.APP_MANAGER_PORT))
 
  server.registerFunction(start_app)
  server.registerFunction(stop_app)
  server.registerFunction(stop_app_instance)
  server.registerFunction(restart_app_instances_for_app)

  file_io.set_logging_format()
  
  while 1:
    try: 
      server.serve_forever()
    except SSL.SSLError: 
      pass
