// UrbanPulse AI – nó IoT de baixo custo (ESP32)
// Sensores: DHT22 (temp/umidade), PMS5003 (PM2.5, UART2), JSN-SR04T/HC-SR04 (nível no bueiro)
// NÃO testado em hardware neste repositório: ajuste pinos e calibração (DEPTH_CM) antes de usar.
#include <WiFi.h>
#include <HTTPClient.h>
#include <DHT.h>

const char* WIFI_SSID = "SUA_REDE";
const char* WIFI_PASS = "SUA_SENHA";
const char* API_URL   = "https://SEU-APP.onrender.com/v1/readings";
const char* SENSOR_ID = "S-001";
const char* DEVICE_KEY = "dev-key-S-001";   // troque em produção

#define DHT_PIN 4
#define TRIG_PIN 5
#define ECHO_PIN 18
#define DEPTH_CM 100.0   // distância do sensor ao fundo do bueiro vazio
DHT dht(DHT_PIN, DHT22);

float readPM25() {                    // frame PMS5003: 32 bytes, header 0x42 0x4D
  Serial2.begin(9600, SERIAL_8N1, 16, 17);
  uint32_t t0 = millis();
  while (millis() - t0 < 2000) {
    if (Serial2.available() >= 32 && Serial2.read() == 0x42 && Serial2.peek() == 0x4D) {
      uint8_t b[32]; b[0] = 0x42;
      Serial2.readBytes(b + 1, 31);
      return (b[12] << 8) | b[13];    // PM2.5 atmosférico (µg/m³)
    }
  }
  return NAN;
}

float readWaterPct() {
  digitalWrite(TRIG_PIN, LOW); delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH); delayMicroseconds(10); digitalWrite(TRIG_PIN, LOW);
  long us = pulseIn(ECHO_PIN, HIGH, 30000);
  if (us == 0) return NAN;
  float dist = us * 0.0343 / 2.0;                       // cm até a superfície
  return constrain((1.0 - dist / DEPTH_CM) * 100.0, 0, 100);
}

void setup() {
  Serial.begin(115200);
  pinMode(TRIG_PIN, OUTPUT); pinMode(ECHO_PIN, INPUT);
  dht.begin();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(500);
}

void loop() {
  String j = String("{\"sensor_id\":\"") + SENSOR_ID + "\"";
  float t = dht.readTemperature(), h = dht.readHumidity(), pm = readPM25(), w = readWaterPct();
  if (!isnan(t))  j += ",\"temperature\":" + String(t, 1);
  if (!isnan(h))  j += ",\"humidity\":" + String(h, 1);
  if (!isnan(pm)) j += ",\"pm25\":" + String(pm, 1);
  if (!isnan(w))  j += ",\"water_level_pct\":" + String(w, 1);
  j += "}";

  HTTPClient http;
  http.begin(API_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_KEY);
  int code = http.POST(j);
  Serial.printf("POST %s -> %d\n", j.c_str(), code);
  http.end();
  delay(30000);                         // 1 leitura a cada 30 s
}
