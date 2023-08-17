from collections import deque
import copy
import math

from cereal import car
from common.conversions import Conversions as CV
from opendbc.can.parser import CANParser
from opendbc.can.can_define import CANDefine
from selfdrive.car.hyundai.hyundaicanfd import CanBus
from selfdrive.car.hyundai.values import HyundaiFlags, CAR, DBC, Buttons, CAN_GEARS, CANFD_CAR, EV_CAR, HEV_CAR, CarControllerParams
from selfdrive.car.interfaces import CarStateBase

PREV_BUTTON_SAMPLES = 8
CLUSTER_SAMPLE_RATE = 20  # frames
STANDSTILL_THRESHOLD = 12 * 0.03125 * CV.KPH_TO_MS


class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)
    can_define = CANDefine(DBC[CP.carFingerprint]["pt"])

    self.cruise_buttons = deque([Buttons.NONE] * PREV_BUTTON_SAMPLES, maxlen=PREV_BUTTON_SAMPLES)
    self.main_buttons = deque([Buttons.NONE] * PREV_BUTTON_SAMPLES, maxlen=PREV_BUTTON_SAMPLES)

    self.gear_msg_canfd = "GEAR_ALT" if CP.flags & HyundaiFlags.CANFD_ALT_GEARS else \
                          "GEAR_ALT_2" if CP.flags & HyundaiFlags.CANFD_ALT_GEARS_2 else \
                          "GEAR_SHIFTER"
    if CP.carFingerprint in CANFD_CAR:
      self.shifter_values = can_define.dv[self.gear_msg_canfd]["GEAR"]
    elif self.CP.carFingerprint in CAN_GEARS["use_cluster_gears"]:
      self.shifter_values = can_define.dv["CLU15"]["CF_Clu_Gear"]
    elif self.CP.carFingerprint in CAN_GEARS["use_tcu_gears"]:
      self.shifter_values = can_define.dv["TCU12"]["CUR_GR"]
    else:  # preferred and elect gear methods use same definition
      self.shifter_values = can_define.dv["LVR12"]["CF_Lvr_Gear"]

    self.accelerator_msg_canfd = "ACCELERATOR" if CP.carFingerprint in EV_CAR else \
                                 "ACCELERATOR_ALT" if CP.carFingerprint in HEV_CAR else \
                                 "ACCELERATOR_BRAKE_ALT"
    self.cruise_btns_msg_canfd = "CRUISE_BUTTONS_ALT" if CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS else \
                                 "CRUISE_BUTTONS"

    #Auto detection for setup
    self.is_metric = False
    self.buttons_counter = 0
    self.eps_error_cnt = 0

    self.cruise_info = {}

    # On some cars, CLU15->CF_Clu_VehicleSpeed can oscillate faster than the dash updates. Sample at 5 Hz
    self.cluster_speed = 0
    self.cluster_speed_counter = CLUSTER_SAMPLE_RATE

    self.params = CarControllerParams(CP)

    self.lfa_btn = 0
    self.lfa_enabled = False

  def update(self, cp, cp_cam):
    if self.CP.carFingerprint in CANFD_CAR:
      return self.update_canfd(cp, cp_cam)

    ret = car.CarState.new_message()
    cp_cruise = cp_cam if self.CP.sccBus == 2 else cp
    self.is_metric = cp.vl["CLU11"]["CF_Clu_SPEED_UNIT"] == 0
    speed_conv = CV.KPH_TO_MS if self.is_metric else CV.MPH_TO_MS

    ret.doorOpen = any([cp.vl["CGW1"]["CF_Gway_DrvDrSw"], cp.vl["CGW1"]["CF_Gway_AstDrSw"],
                        cp.vl["CGW2"]["CF_Gway_RLDrSw"], cp.vl["CGW2"]["CF_Gway_RRDrSw"]])

    ret.seatbeltUnlatched = cp.vl["CGW1"]["CF_Gway_DrvSeatBeltSw"] == 0

    ret.wheelSpeeds = self.get_wheel_speeds(cp.vl["WHL_SPD11"]["WHL_SPD_FL"], cp.vl["WHL_SPD11"]["WHL_SPD_FR"],
                                            cp.vl["WHL_SPD11"]["WHL_SPD_RL"], cp.vl["WHL_SPD11"]["WHL_SPD_RR"])

    ret.vEgoRaw = (ret.wheelSpeeds.fl + ret.wheelSpeeds.fr + ret.wheelSpeeds.rl + ret.wheelSpeeds.rr) / 4.
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)
    ret.standstill = ret.wheelSpeeds.fl <= STANDSTILL_THRESHOLD and ret.wheelSpeeds.rr <= STANDSTILL_THRESHOLD

    self.cluster_speed_counter += 1
    if self.cluster_speed_counter > CLUSTER_SAMPLE_RATE:
      self.cluster_speed = cp.vl["CLU15"]["CF_Clu_VehicleSpeed"]
      self.cluster_speed_counter = 0

      # Mimic how dash converts to imperial.
      if not self.is_metric:
        self.cluster_speed = math.floor(self.cluster_speed * CV.KPH_TO_MPH + CV.KPH_TO_MPH)

    ret.vEgoCluster = self.cluster_speed * speed_conv

    ret.steeringAngleDeg = cp.vl["SAS11"]["SAS_Angle"]
    ret.steeringRateDeg = cp.vl["SAS11"]["SAS_Speed"]
    ret.steeringTorque = cp.vl["MDPS12"]["CR_Mdps_StrColTq"]
    ret.steeringTorqueEps = cp.vl["MDPS12"]["CR_Mdps_OutTq"]
    ret.steeringPressed = abs(ret.steeringTorque) > self.params.STEER_THRESHOLD
    self.eps_error_cnt += 1 if not ret.standstill and cp.vl["MDPS12"]["CF_Mdps_ToiUnavail"] != 0 else -self.eps_error_cnt
    ret.steerFaultTemporary = self.eps_error_cnt > 100

    ret.yawRate = cp.vl["ESP12"]["YAW_RATE"]
    ret.leftBlinker, ret.rightBlinker = self.update_blinker_from_lamp(50, cp.vl["CGW1"]["CF_Gway_TurnSigLh"],
                                                                      cp.vl["CGW1"]["CF_Gway_TurnSigRh"])

    # cruise state
    if self.CP.openpilotLongitudinalControl and self.CP.sccBus == 0:
      # These are not used for engage/disengage since openpilot keeps track of state using the buttons
      ret.cruiseState.available = cp.vl["TCS13"]["ACCEnable"] == 0
      ret.cruiseState.enabled = cp.vl["TCS13"]["ACC_REQ"] == 1
      ret.cruiseState.standstill = False
    elif self.CP.sccBus == -1:
      ret.cruiseState.available = cp.vl["EMS16"]["CRUISE_LAMP_M"] != 0
      ret.cruiseState.enabled = cp.vl["LVR12"]["CF_Lvr_CruiseSet"] != 0
      ret.cruiseState.standstill = False
      ret.cruiseState.speed = cp.vl["LVR12"]["CF_Lvr_CruiseSet"] * speed_conv if ret.cruiseState.enabled else 0
    else:
      ret.cruiseState.available = cp_cruise.vl["SCC11"]["MainMode_ACC"] == 1
      ret.cruiseState.enabled = cp_cruise.vl["SCC12"]["ACCMode"] != 0
      ret.cruiseState.standstill = cp_cruise.vl["SCC11"]["SCCInfoDisplay"] == 4.
      ret.cruiseState.speed = cp_cruise.vl["SCC11"]["VSetDis"] * speed_conv if ret.cruiseState.enabled else 0
      ret.cruiseState.gapAdjust = cp_cruise.vl["SCC11"]["TauGapSet"]

    # TODO: Find brake pressure
    ret.brake = 0
    ret.brakePressed = cp.vl["TCS13"]["DriverBraking"] != 0
    ret.brakeHoldActive = cp.vl["TCS15"]["AVH_LAMP"] == 2  # 0 OFF, 1 ERROR, 2 ACTIVE, 3 READY
    ret.parkingBrake = cp.vl["TCS13"]["PBRAKE_ACT"] == 1
    ret.brakeLights = bool(ret.brakePressed)
    ret.accFaulted = cp.vl["TCS13"]["ACCEnable"] != 0  # 0 ACC CONTROL ENABLED, 1-3 ACC CONTROL DISABLED

    if self.CP.carFingerprint in (EV_CAR | HEV_CAR):
      if self.CP.carFingerprint in HEV_CAR:
        ret.gas = cp.vl["E_EMS11"]["CR_Vcu_AccPedDep_Pos"] / 254.
      else:
        ret.gas = cp.vl["E_EMS11"]["Accel_Pedal_Pos"] / 254.
      ret.gasPressed = ret.gas > 0
    else:
      ret.gas = cp.vl["EMS12"]["PV_AV_CAN"] / 100.
      ret.gasPressed = bool(cp.vl["EMS16"]["CF_Ems_AclAct"])

    # Gear Selection via Cluster - For those Kia/Hyundai which are not fully discovered, we can use the Cluster Indicator for Gear Selection,
    # as this seems to be standard over all cars, but is not the preferred method.
    if self.CP.carFingerprint in CAN_GEARS["use_cluster_gears"]:
      gear = cp.vl["CLU15"]["CF_Clu_Gear"]
    elif self.CP.carFingerprint in CAN_GEARS["use_tcu_gears"]:
      gear = cp.vl["TCU12"]["CUR_GR"]
    elif self.CP.carFingerprint in CAN_GEARS["use_elect_gears"]:
      if self.CP.carFingerprint == CAR.NEXO:
        gear = cp.vl["ELECT_GEAR"]["Elect_Gear_Shifter_NEXO"]
      else:
        gear = cp.vl["ELECT_GEAR"]["Elect_Gear_Shifter"]
    else:
      gear = cp.vl["LVR12"]["CF_Lvr_Gear"]

    ret.gearShifter = self.parse_gear_shifter(self.shifter_values.get(gear))

    if not self.CP.openpilotLongitudinalControl or self.CP.sccBus == 2:
      aeb_src = "FCA11" if self.CP.flags & HyundaiFlags.USE_FCA.value else "SCC12"
      aeb_sig = "FCA_CmdAct" if self.CP.flags & HyundaiFlags.USE_FCA.value else "AEB_CmdAct"
      aeb_warning = cp_cruise.vl[aeb_src]["CF_VSM_Warn"] != 0
      aeb_braking = cp_cruise.vl[aeb_src]["CF_VSM_DecCmdAct"] != 0 or cp_cruise.vl[aeb_src][aeb_sig] != 0
      ret.stockFcw = aeb_warning and not aeb_braking
      ret.stockAeb = aeb_warning and aeb_braking

    if self.CP.enableBsm:
      ret.leftBlindspot = cp.vl["LCA11"]["CF_Lca_IndLeft"] != 0
      ret.rightBlindspot = cp.vl["LCA11"]["CF_Lca_IndRight"] != 0

    # save the entire LKAS11, CLU11, MDPS12, LFAHDA_MFC, SCC11, SCC12, SCC13, SCC14
    self.lkas11 = copy.copy(cp_cam.vl["LKAS11"])
    self.clu11 = copy.copy(cp.vl["CLU11"])
    self.mdps12 = copy.copy(cp.vl["MDPS12"])
    self.scc11 = copy.copy(cp_cruise.vl["SCC11"])
    self.scc12 = copy.copy(cp_cruise.vl["SCC12"])
    self.scc13 = copy.copy(cp_cruise.vl["SCC13"]) if self.CP.hasScc13 else None
    self.scc14 = copy.copy(cp_cruise.vl["SCC14"]) if self.CP.hasScc14 else None
    self.fca11 = cp.vl["FCA11"]
    self.fca12 = cp.vl["FCA12"]
    self.mfc_lfa = cp_cam.vl["LFAHDA_MFC"]

    self.steer_state = cp.vl["MDPS12"]["CF_Mdps_ToiActive"]  # 0 NOT ACTIVE, 1 ACTIVE
    self.prev_cruise_buttons = self.cruise_buttons[-1]
    self.cruise_buttons.extend(cp.vl_all["CLU11"]["CF_Clu_CruiseSwState"])
    self.main_buttons.extend(cp.vl_all["CLU11"]["CF_Clu_CruiseSwMain"])
    self.lead_distance = cp_cruise.vl["SCC11"]["ACC_ObjDist"]

    tpms_unit = cp.vl["TPMS11"]["UNIT"] * 0.725 if int(cp.vl["TPMS11"]["UNIT"]) > 0 else 1.
    ret.tpms.fl = tpms_unit * cp.vl["TPMS11"]["PRESSURE_FL"]
    ret.tpms.fr = tpms_unit * cp.vl["TPMS11"]["PRESSURE_FR"]
    ret.tpms.rl = tpms_unit * cp.vl["TPMS11"]["PRESSURE_RL"]
    ret.tpms.rr = tpms_unit * cp.vl["TPMS11"]["PRESSURE_RR"]

    if self.CP.hasAutoHold:
      ret.autoHold = cp.vl["ESP11"]["AVH_STAT"]

    if self.CP.hasNav:
      ret.navLimitSpeed = cp.vl["Navi_HU"]["SpeedLim_Nav_Clu"]

    if self.CP.hasLfa:
      prev_lfa_btn = self.lfa_btn
      self.lfa_btn = cp.vl["BCM_PO_11"]["LFA_Pressed"]
      if prev_lfa_btn != 1 and self.lfa_btn == 1:
        self.lfa_enabled = not self.lfa_enabled

      ret.cruiseState.available = self.lfa_enabled

    return ret


  def update_canfd(self, cp, cp_cam):
    ret = car.CarState.new_message()

    self.is_metric = cp.vl["CRUISE_BUTTONS_ALT"]["DISTANCE_UNIT"] != 1
    speed_factor = CV.KPH_TO_MS if self.is_metric else CV.MPH_TO_MS

    if self.CP.carFingerprint in (EV_CAR | HEV_CAR):
      offset = 255. if self.CP.carFingerprint in EV_CAR else 1023.
      ret.gas = cp.vl[self.accelerator_msg_canfd]["ACCELERATOR_PEDAL"] / offset
      ret.gasPressed = ret.gas > 1e-5
    else:
      ret.gasPressed = bool(cp.vl[self.accelerator_msg_canfd]["ACCELERATOR_PEDAL_PRESSED"])

    ret.brakePressed = cp.vl["TCS"]["DriverBraking"] == 1
    ret.brakeLights = bool(ret.brakePressed)

    ret.doorOpen = cp.vl["DOORS_SEATBELTS"]["DRIVER_DOOR"] == 1
    ret.seatbeltUnlatched = cp.vl["DOORS_SEATBELTS"]["DRIVER_SEATBELT"] == 0

    gear = cp.vl[self.gear_msg_canfd]["GEAR"]
    ret.gearShifter = self.parse_gear_shifter(self.shifter_values.get(gear))

    # TODO: figure out positions
    ret.wheelSpeeds = self.get_wheel_speeds(cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_1"], cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_2"],
                                            cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_3"], cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_4"])

    ret.vEgoRaw = (ret.wheelSpeeds.fl + ret.wheelSpeeds.fr + ret.wheelSpeeds.rl + ret.wheelSpeeds.rr) / 4.
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)
    ret.standstill = ret.wheelSpeeds.fl <= STANDSTILL_THRESHOLD and ret.wheelSpeeds.rr <= STANDSTILL_THRESHOLD

    ret.steeringRateDeg = cp.vl["STEERING_SENSORS"]["STEERING_RATE"]
    ret.steeringAngleDeg = cp.vl["STEERING_SENSORS"]["STEERING_ANGLE"] * -1
    ret.steeringTorque = cp.vl["MDPS"]["STEERING_COL_TORQUE"]
    ret.steeringTorqueEps = cp.vl["MDPS"]["STEERING_OUT_TORQUE"]
    ret.steeringPressed = self.update_steering_pressed(abs(ret.steeringTorque) > self.params.STEER_THRESHOLD, 5)
    ret.steerFaultTemporary = cp.vl["MDPS"]["LKA_FAULT"] != 0

    ret.leftBlinker, ret.rightBlinker = self.update_blinker_from_lamp(50, cp.vl["BLINKERS"]["LEFT_LAMP"],
                                                                      cp.vl["BLINKERS"]["RIGHT_LAMP"])
    if self.CP.enableBsm:
      ret.leftBlindspot = cp.vl["BLINDSPOTS_REAR_CORNERS"]["FL_INDICATOR"] != 0
      ret.rightBlindspot = cp.vl["BLINDSPOTS_REAR_CORNERS"]["FR_INDICATOR"] != 0

    # cruise state
    # CAN FD cars enable on main button press, set available if no TCS faults preventing engagement
    ret.cruiseState.available = cp.vl["TCS"]["ACCEnable"] == 0
    if self.CP.openpilotLongitudinalControl:
      # These are not used for engage/disengage since openpilot keeps track of state using the buttons
      ret.cruiseState.enabled = cp.vl["TCS"]["ACC_REQ"] == 1
      ret.cruiseState.standstill = False
    else:
      cp_cruise_info = cp_cam if self.CP.flags & HyundaiFlags.CANFD_CAMERA_SCC else cp
      ret.cruiseState.enabled = cp_cruise_info.vl["SCC_CONTROL"]["ACCMode"] in (1, 2)
      ret.cruiseState.standstill = cp_cruise_info.vl["SCC_CONTROL"]["CRUISE_STANDSTILL"] == 1
      ret.cruiseState.speed = cp_cruise_info.vl["SCC_CONTROL"]["VSetDis"] * speed_factor
      self.cruise_info = copy.copy(cp_cruise_info.vl["SCC_CONTROL"])

    self.prev_cruise_buttons = self.cruise_buttons[-1]
    self.cruise_buttons.extend(cp.vl_all[self.cruise_btns_msg_canfd]["CRUISE_BUTTONS"])
    self.main_buttons.extend(cp.vl_all[self.cruise_btns_msg_canfd]["ADAPTIVE_CRUISE_MAIN_BTN"])
    self.buttons_counter = cp.vl[self.cruise_btns_msg_canfd]["COUNTER"]
    ret.accFaulted = cp.vl["TCS"]["ACCEnable"] != 0  # 0 ACC CONTROL ENABLED, 1-3 ACC CONTROL DISABLED

    if self.CP.flags & HyundaiFlags.CANFD_HDA2:
      self.cam_0x2a4 = copy.copy(cp_cam.vl["CAM_0x2a4"])

    if self.CP.hasNav:
      ret.navLimitSpeed = cp.vl["CLUSTER_SPEED_LIMIT"]["SPEED_LIMIT_1"]

    prev_lfa_btn = self.lfa_btn
    self.lfa_btn = cp.vl[self.cruise_btns_msg_canfd]["LFA_BTN"]
    if prev_lfa_btn != 1 and self.lfa_btn == 1:
      self.lfa_enabled = not self.lfa_enabled

    ret.cruiseState.available = self.lfa_enabled

    return ret


  def get_can_parser(self, CP):
    if CP.carFingerprint in CANFD_CAR:
      return self.get_can_parser_canfd(CP)

    messages = [
      # address, frequency
      ("MDPS12", 50),
      ("TCS13", 50),
      ("TCS15", 10),
      ("CLU11", 50),
      ("CLU15", 5),
      ("ESP12", 100),
      ("CGW1", 10),
      ("CGW2", 5),
      ("CGW4", 5),
      ("WHL_SPD11", 50),
      ("SAS11", 100),
      ("TPMS11", 0),
    ]

    if CP.sccBus == 0:
      messages += [
        ("SCC11", 50),
        ("SCC12", 50),
      ]

      if CP.hasScc13:
        messages.append(("SCC13", 50))

      if CP.hasScc14:
        messages.append(("SCC14", 50))

    if not CP.openpilotLongitudinalControl:
      if not CP.sccBus == -1:
        messages += [
          ("SCC11", 50),
          ("SCC12", 50),
        ]
      elif CP.sccBus == -1:
        pass

      if CP.flags & HyundaiFlags.USE_FCA.value:
        messages.append(("FCA11", 50))
      elif not CP.sccBus == -1:
        pass

    if CP.enableBsm:
      messages.append(("LCA11", 50))

    if CP.hasAutoHold:
      messages.append(("ESP11", 50))

    if CP.hasNav:
      messages.append(("Navi_HU", 5))

    if CP.hasLfa:
      messages.append(("BCM_PO_11", 50))

    if CP.carFingerprint in (EV_CAR | HEV_CAR):
      messages.append(("E_EMS11", 50))
    else:
      messages += [
        ("EMS12", 100),
        ("EMS16", 100),
      ]

    if CP.carFingerprint in CAN_GEARS["use_cluster_gears"]:
      pass
    elif CP.carFingerprint in CAN_GEARS["use_tcu_gears"]:
      messages.append(("TCU12", 100))
    elif CP.carFingerprint in CAN_GEARS["use_elect_gears"]:
      messages.append(("ELECT_GEAR", 20))
    else:
      messages.append(("LVR12", 100))

    return CANParser(DBC[CP.carFingerprint]["pt"], messages, 0)

  @staticmethod
  def get_cam_can_parser(CP):
    if CP.carFingerprint in CANFD_CAR:
      return CarState.get_cam_can_parser_canfd(CP)

    messages = [
      ("LKAS11", 100)
    ]

    if CP.openpilotLongitudinalControl and CP.sccBus == 2:
      messages += [
        ("SCC11", 50),
        ("SCC12", 50),
      ]

      if CP.hasScc13:
        messages.append(("SCC13", 50))

      if CP.hasScc14:
        messages.append(("SCC14", 50))

    if not CP.openpilotLongitudinalControl:
      messages += [
        ("SCC11", 50),
        ("SCC12", 50),
      ]

      if CP.flags & HyundaiFlags.USE_FCA.value:
        messages.append(("FCA11", 50))
      elif CP.sccBus == -1:
        pass

    return CANParser(DBC[CP.carFingerprint]["pt"], messages, 2)

  def get_can_parser_canfd(self, CP):
    messages = [
      (self.gear_msg_canfd, 100),
      (self.cruise_btns_msg_canfd, 50),
      (self.accelerator_msg_canfd, 100),
      ("WHEEL_SPEEDS", 100),
      ("STEERING_SENSORS", 100),
      ("MDPS", 100),
      ("TCS", 50),
      ("CRUISE_BUTTONS_ALT", 50),
      ("BLINKERS", 4),
      ("DOORS_SEATBELTS", 4),
    ]

    if CP.enableBsm:
      messages += [
        ("BLINDSPOTS_REAR_CORNERS", 20),
      ]

    if not (CP.flags & HyundaiFlags.CANFD_CAMERA_SCC.value) and not CP.openpilotLongitudinalControl:
      messages += [
        ("SCC_CONTROL", 50),
      ]

    if CP.flags & HyundaiFlags.CANFD_HDA2 and CP.hasNav:
      messages.append(("CLUSTER_SPEED_LIMIT", 10))

    return CANParser(DBC[CP.carFingerprint]["pt"], messages, CanBus(CP).ECAN)

  @staticmethod
  def get_cam_can_parser_canfd(CP):
    messages = []
    if CP.flags & HyundaiFlags.CANFD_HDA2:
      messages += [("CAM_0x2a4", 20)]
    elif CP.flags & HyundaiFlags.CANFD_CAMERA_SCC:
      messages += [
        ("SCC_CONTROL", 50),
      ]

    if not (CP.flags & HyundaiFlags.CANFD_HDA2) and CP.hasNav:
      messages.append(("CLUSTER_SPEED_LIMIT", 10))

    return CANParser(DBC[CP.carFingerprint]["pt"], messages, CanBus(CP).CAM)
