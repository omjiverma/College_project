import json
from datetime import datetime
from time import sleep as wait

import serial
from simglucose.controller.base import Controller, Action


class ESP32_Controller(Controller):

    def __init__(self, port="COM4", baud=115200, logger=None):
        self.logger = logger
        self.port = port
        self.baud = baud

        # Open Bluetooth serial connection
        self.ser = serial.Serial(self.port, self.baud, timeout=5)
        wait(10)
        print(f"Connected to ESP32 on {self.port} (baud={self.baud})")

        self.reset()


    def reset(self):
        self.last_cgm = None
        print("Controller reset")


    def policy(self, observation, reward=None, done=None, **info):

        if info.get("new_episode", False):
            self.reset()

        raw_cgm = float(observation.CGM)
        meal_cho = info.get("meal", 0.0)
        now = info.get("time", datetime.now())

        packet = {
            "timestamp": int(now.timestamp()),
            "CGM": raw_cgm,
            "CHO": meal_cho
        }

        message = json.dumps(packet) + "\n"
        data = {}

        try:
            # Send packet to ESP32
            self.ser.write(message.encode())
            self.ser.flush()

            print("Sent to ESP32:", packet)

            # Receive response
            response = self.ser.readline().decode().strip()

            if response:
                data = json.loads(response)
                print("Received from ESP32:", data)
            else:
                print("No response from ESP32")

        except Exception as e:
            print("Serial communication error:", e)

        # No control logic yet
        basal = data.get("basal", 0.0)
        bolus = data.get("bolus", 0.0)

        if self.logger:
            self.logger.log_step(
                step=info.get("step", 0),
                time=now,
                cgm=raw_cgm,
                basal=basal,
                bolus=bolus,
                iob=0.0,
                iob_model="ESP32",
                cho=meal_cho,
                aggression=1.0,
                trend=0.0,
                target=120.0,
                esp32_response=bool(data)
            )

        return Action(basal=basal, bolus=bolus)
