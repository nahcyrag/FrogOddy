import datetime
import http.client
import os
import socket
import threading
import urllib.error
import urllib.request

from cereal import log, messaging
from openpilot.common.params import Params
from openpilot.common.realtime import Priority, config_realtime_process
from openpilot.common.time import system_time_valid
from openpilot.system.hardware import HARDWARE

from openpilot.selfdrive.frogpilot.controls.frogpilot_planner import FrogPilotPlanner
from openpilot.selfdrive.frogpilot.controls.lib.frogpilot_functions import FrogPilotFunctions
from openpilot.selfdrive.frogpilot.controls.lib.frogpilot_variables import FrogPilotVariables
from openpilot.selfdrive.frogpilot.controls.lib.model_manager import DEFAULT_MODEL, DEFAULT_MODEL_NAME, download_model, populate_models
from openpilot.selfdrive.frogpilot.controls.lib.theme_manager import ThemeManager

OFFLINE = log.DeviceState.NetworkType.none

def github_pinged(url="https://github.com", timeout=5):
  try:
    urllib.request.urlopen(url, timeout=timeout)
    return True
  except (urllib.error.URLError, socket.timeout, http.client.RemoteDisconnected):
    return False

def automatic_update_check(started, params):
  update_available = params.get_bool("UpdaterFetchAvailable")
  update_ready = params.get_bool("UpdateAvailable")
  update_state_idle = params.get("UpdaterState", encoding='utf8') == "idle"

  if update_ready and not started:
    HARDWARE.reboot()
  elif update_available:
    os.system("pkill -SIGHUP -f system.updated.updated")
  elif update_state_idle:
    os.system("pkill -SIGUSR1 -f system.updated.updated")

def time_checks(automatic_updates, deviceState, now, started, params, params_memory):
  if deviceState.networkType == OFFLINE:
    return

  if not github_pinged():
    return

  screen_off = deviceState.screenBrightnessPercent == 0
  if automatic_updates and screen_off:
    automatic_update_check(started, params)

  populate_models()
  update_maps(now, params, params_memory)

def update_maps(now, params, params_memory):
  maps_selected = params.get("MapsSelected", encoding='utf8')
  if maps_selected is None:
    return

  day = now.day
  is_first = day == 1
  is_Sunday = now.weekday() == 6
  schedule = params.get_int("PreferredSchedule")

  maps_downloaded = os.path.exists('/data/media/0/osm/offline')
  if maps_downloaded and (schedule == 0 or (schedule == 1 and not is_Sunday) or (schedule == 2 and not is_first)):
    return

  suffix = "th" if 4 <= day <= 20 or 24 <= day <= 30 else ["st", "nd", "rd"][day % 10 - 1]
  todays_date = now.strftime(f"%B {day}{suffix}, %Y")

  if params.get("LastMapsUpdate", encoding='utf-8') == todays_date:
    return

  if params.get("OSMDownloadProgress", encoding='utf-8') is None:
    params_memory.put_nonblocking("OSMDownloadLocations", maps_selected)
    params.put_nonblocking("LastMapsUpdate", todays_date)

def frogpilot_thread():
  config_realtime_process(5, Priority.CTRL_LOW)

  frogpilot_toggles = FrogPilotVariables.toggles

  params = Params()
  params_memory = Params("/dev/shm/params")

  frogpilot_functions = FrogPilotFunctions()
  frogpilot_planner = FrogPilotPlanner()
  theme_manager = ThemeManager()

  downloading_model = False
  run_time_checks = False
  time_validated = system_time_valid()
  update_toggles = False

  pm = messaging.PubMaster(['frogpilotPlan'])
  sm = messaging.SubMaster(['carState', 'controlsState', 'deviceState', 'frogpilotCarControl',
                            'frogpilotCarState', 'frogpilotNavigation', 'modelV2', 'radarState'],
                            poll='modelV2', ignore_avg_freq=['radarState'])

  while True:
    sm.update()

    now = datetime.datetime.now()
    deviceState = sm['deviceState']
    started = deviceState.started

    if started and sm.updated['modelV2']:
      frogpilot_planner.update(sm['carState'], sm['controlsState'], sm['frogpilotCarControl'], sm['frogpilotCarState'],
                               sm['frogpilotNavigation'], sm['modelV2'], sm['radarState'], frogpilot_toggles)
      frogpilot_planner.publish(sm, pm, frogpilot_toggles)

    model_to_download = params_memory.get("ModelToDownload", encoding='utf-8')
    if model_to_download is not None:
      if not downloading_model:
        threading.Thread(target=download_model).start()
        downloading_model = True
    else:
      downloading_model = False

    if FrogPilotVariables.toggles_updated:
      update_toggles = True
    elif update_toggles:
      threading.Thread(target=FrogPilotVariables.update_frogpilot_params, args=(started,)).start()

      if not frogpilot_toggles.model_selector:
        params.put_nonblocking("Model", DEFAULT_MODEL)
        params.put_nonblocking("ModelName", DEFAULT_MODEL_NAME)

      if time_validated and not started:
        threading.Thread(target=frogpilot_functions.backup_toggles).start()

      update_toggles = False

    if now.second == 0:
      run_time_checks = True
    elif run_time_checks or not time_validated:
      threading.Thread(target=time_checks, args=(frogpilot_toggles.automatic_updates, deviceState, now, started, params, params_memory,)).start()
      run_time_checks = False

      if not time_validated:
        time_validated = system_time_valid()
        if not time_validated:
          continue

      theme_manager.update_holiday()

def main():
  frogpilot_thread()

if __name__ == "__main__":
  main()
