import math
import time
import urequests as requests
import network
import ssd1306
from machine import Pin, I2C, ADC
from fifo import Fifo
from piotimer import Piotimer
import ujson
import uerrno
import usocket as socket
from umqtt.simple import MQTTClient


class isr_adc:
    def __init__(self, adc_pin_nr):
        self.av = ADC(adc_pin_nr)
        self.fifo = Fifo(500)

    def handler(self, tid):
        self.fifo.put(self.av.read_u16())

class InterruptButton:
    def __init__(self, button_pin, fifo):
        self.button = Pin(button_pin, mode=Pin.IN, pull=Pin.PULL_UP)
        self.nr = button_pin
        self.fifo = fifo
        self.button.irq(handler=self.handler, trigger=Pin.IRQ_FALLING, hard=True)
    
    def handler(self, pin):
        self.fifo.put(self.nr)

class HeartRateMonitor:
    def __init__(self):
        # Initialize components
        self.events = Fifo(30)
        self.sw0 = InterruptButton(8, self.events)
        self.sw1 = InterruptButton(9, self.events)
        self.i2c = I2C(1, sda=Pin("GP14"), scl=Pin("GP15"))
        self.display = ssd1306.SSD1306_I2C(128, 64, self.i2c)
        self.adc_pin_nr = 26
        self.sample_rate = 250
        self.threshold_percentage = 0.15
        self.adc = isr_adc(self.adc_pin_nr)
        self.raw_data_list = []
        self.peak_interval_list = []
        self.measurement_started = False
        self.start_time = None
        self.end_time = None
        self.previous_sample = 0
        self.tmr = None
        self.start_button = Pin("GP9", Pin.IN, Pin.PULL_UP)
        self.stop_button = Pin("GP7", Pin.IN, Pin.PULL_UP)
        self.last_button_press = 0
        self.mqtt_broker_ip = "192.168.9.253"
        self.mqtt_topic = "pico/test"
        self.mqtt_client_id = "micropython"

    def connect_wifi(self):
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.connect("KMD652_Group_9", "metropoliagroup9")
        print("Connecting to WiFi...")
        while not wlan.isconnected():
            pass
        print("WiFi Connected!")
        print("IP Address:", wlan.ifconfig()[0])



    def measure_heart_rate(self):
        print("Measurement started...")
        self.start_time = time.time()
        tmr = Piotimer(mode=Piotimer.PERIODIC, freq=self.sample_rate, callback=self.adc.handler)
        while self.measurement_started:
            if self.adc.fifo.has_data():
                value_read = self.adc.fifo.get()
                self.raw_data_list.append(value_read)
                if len(self.raw_data_list) >= 750:
                    min_value = min(self.raw_data_list)
                    max_value = max(self.raw_data_list)
                    amplitude = max_value - min_value
                    threshold = max_value - amplitude * self.threshold_percentage

                    for item in range(1, len(self.raw_data_list) - 1):
                        if self.raw_data_list[item] > threshold and self.raw_data_list[item - 1] < self.raw_data_list[item] and self.raw_data_list[item] > self.raw_data_list[item + 1]:
                            peak_interval = (item - self.previous_sample) * 4
                            if 700 <= peak_interval <= 1200:
                                heart_rate = round(60000 / peak_interval)
                                if 30 <= heart_rate <= 240:
                                    self.peak_interval_list.append(peak_interval)
                                    print(f"PPI: {peak_interval}, Heart rate: {heart_rate} bpm")
                                    #self.publish_mqtt_message({"ppi": peak_interval, "heart_rate": heart_rate})
                                    print("")
                                    self.display.fill(0)
                                    self.display.text(f"  BPM: {heart_rate}  ", 0, 10)
                                    self.display.text("---------------", 0, 20)
                                    self.display.text("   Measuring   ", 0, 30)
                                    self.display.show()

                    self.raw_data_list = []
                    
                    if time.time() - self.start_time >= 25:
                        self.end_time = time.time()
                        self.stop_measurement()
                        self.calculate_hrv_parameters()

    def start_measurement(self):
        self.measurement_started = True
        self.measure_heart_rate()

    def stop_measurement(self):
        self.measurement_started = False

    def start(self):
        self.connect_wifi()
        self.setup_mqtt()
        print("Press button 9 to start measurement ...")
        self.display.fill(0)
        self.display.text('Press To Start', 0, 10)
        self.display.text('<--------', 0, 40)
        self.display.show()
        while True:
            if not self.start_button.value():  # Check if start button is pressed
                if time.time() - self.last_button_press > 0.5:  # Debounce delay
                    print("Button 9 pressed! Starting measurement...")
                    self.display.fill(0)
                    self.display.show()
                    time.sleep(0.5)
                    self.display.text("..Starting..", 0,10)
                    self.display.show()
                    self.last_button_press = time.time()
                    self.start_measurement()

            if not self.stop_button.value():  # Check if stop button is pressed
                if time.time() - self.last_button_press > 0.5:  # Debounce delay
                    print("Button 7 pressed! Stopping measurement and printing results...")
                    self.last_button_press = time.time()
                    self.stop_measurement()
                    self.calculate_hrv_parameters()

    def calculate_sdnn(self, peak_intervals):
        mean_ppi = sum(peak_intervals) / len(peak_intervals)
        differences = [(ppi - mean_ppi) ** 2 for ppi in peak_intervals]
        variance = sum(differences) / (len(peak_intervals) - 1)
        sdnn = math.sqrt(variance)
        return sdnn
    
    def calculate_rmssd(self, peak_intervals):
        differences = [peak_intervals[i+1] - peak_intervals[i] for i in range(len(peak_intervals) - 1)]
        squared_diff = [diff ** 2 for diff in differences]
        mean_squared_diff = sum(squared_diff) / len(squared_diff)
        rmssd = math.sqrt(mean_squared_diff)
        return rmssd
    
    def calculate_hrv_parameters(self):
        mean_ppi = sum(self.peak_interval_list) / len(self.peak_interval_list)
        mean_hr = 60000 / mean_ppi
        sdnn_value = self.calculate_sdnn(self.peak_interval_list)
        rmssd_value = self.calculate_rmssd(self.peak_interval_list)
        
        print("Basic HRV Analysis Parameters:")
        print(f"Mean PPI: {round(mean_ppi)} ms")
        print(f"Mean HR: {round(mean_hr)} bpm")
        print(f"SDNN: {round(sdnn_value, 2)} ms")
        print(f"RMSSD: {round(rmssd_value, 2)} ms")
        
        # Prepare the data set in the required format for the Kubios API call
        data_set = {
            "type": "RRI",
            "data": self.peak_interval_list,
            "analysis": {
                "type": "readiness"
            }
        }
        
        try:
            # Request an access token from Kubios API using the credentials
            response = requests.post(
                url="https://kubioscloud.auth.eu-west-1.amazoncognito.com/oauth2/token",
                data='grant_type=client_credentials&client_id=3pjgjdmamlj759te85icf0lucv',
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                auth=('3pjgjdmamlj759te85icf0lucv', '111fqsli1eo7mejcrlffbklvftcnfl4keoadrdv1o45vt9pndlef'))
            response = response.json()
            access_token = response["access_token"]

            # Call the Kubios API with the prepared data set and the access token obtained earlier
            response = requests.post(
                url="https://analysis.kubioscloud.com/v2/analytics/analyze",
                headers={"Authorization": "Bearer {}".format(access_token),
                         "X-Api-Key": "pbZRUi49X48I56oL1Lq8y8NDjq6rPfzX3AQeNo3a"},
                json=data_set)
            response = response.json()
            print("response", response)

            analysis_dictionary = response['analysis']
            SNS = analysis_dictionary['sns_index']
            PNS = analysis_dictionary['pns_index']

            print(f"SNS index: {round(SNS, 3)}")
            print(f"PNS index: {round(PNS, 3)}")

            self.display.fill(0)
            self.display.text(f'SNS: {round(SNS, 3)}', 0, 10)
            self.display.text(f'PNS: {round(PNS, 3)}', 0, 20)
            self.display.show()
            
            if (5 < SNS < 35) or (PNS < -2):
                self.display.text("STRESSED OUT !!!", 20, 40)
                self.display.show()
            else:
                self.display.text("NORMAL !!!", 30, 40)
                self.display.show()

            # Publish MQTT message after all calculations are done
            mqtt_message = {
                "mean_ppi": round(mean_ppi),
                "mean_hr": round(mean_hr),
                "sdnn": round(sdnn_value, 2),
                "rmssd": round(rmssd_value, 2),
                "sns_index": round(SNS, 3),
                "pns_index": round(PNS, 3)
            }
            self.publish_mqtt_message(mqtt_message)

        except Exception as e:
            print("Error in Kubios API call:", e)

    def setup_mqtt(self):
        self.client = MQTTClient(self.mqtt_client_id, self.mqtt_broker_ip)
        self.client.connect()
        print("Connected to MQTT broker")

    def publish_mqtt_message(self, message):
        try:
            json_message = ujson.dumps(message)  # Convert message to JSON string
            self.client.publish(self.mqtt_topic, json_message.encode())  # Encode JSON string to bytes and publish
            print("Published MQTT message:", message)
        except OSError as e:
            print("Failed to publish MQTT message:", e)

# Initialize and start the heart rate monitor
heart_rate_monitor = HeartRateMonitor()
heart_rate_monitor.start()

