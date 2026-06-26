#

docker run --rm -it   -v ./mqtt/config:/mosquitto/config   eclipse-mosquitto   mosquitto_passwd -c /mosquitto/config/pwfile USER

recorder:
  db_url: mysql://homeassistant:PW@127.0.0.1/homeassistant?charset=utf8mb4
  purge_keep_days: 90
  auto_purge: true
