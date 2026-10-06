# Home Assistant Docker

Home Assistant Container, MariaDB recorder, TLS MQTT és Node-RED egy Linux
gépen. A szolgáltatások host hálózatot használnak. A MariaDB és a Node-RED csak
`127.0.0.1` címen figyel; a Home Assistant a 8123-as, az MQTT a 8883-as porton érhető el.
A Node-RED szerkesztő és HTTP node-ok hitelesítést kérnek. Az MQTT anonim
kapcsolatot nem enged, TLS-t és topic-jogosultságokat használ; nincs 1883-as
titkosítatlan vagy 9001-es WebSocket listener.

Raspberry Pi és SD-kártya használatához minden szolgáltatás Docker logging
drivere `none`: a Docker nem tárolja a konténerek stdout/stderr naplóit, ezért
a `docker compose logs` nem használható. Az alkalmazások saját fájljai és
adatbázisai ettől még írhatnak a kártyára.

## Követelmények

- Linux, Docker Engine 23 vagy újabb, Docker Compose v2.20 vagy újabb; Docker
  Desktop helyett natív Docker Engine. A gépen Docker daemon hozzáférés kell.
- Python 3.9+, OpenSSL és SSH; könyvtárjogosultságokhoz `sudo`.
- Szabad 3306, 8123, 8883 és 1880 port; elegendő memória és tárhely.
  A recorder 90 napos megőrzése függ az entitások számától; adatbázis-karbantartáshoz
  az adatbázis méretével legalább azonos szabad tárhelyet is tarts fenn.
- A hoston legyen `/run/dbus`, ha Bluetooth-integrációt használsz.
  A `privileged` mód a Home Assistant hardveres integrációihoz széles hosthozzáférést
  ad; csak megbízható image-ekkel és védett kezelőfelülettel használd.

A támogatott távoli hozzáférés a Node-RED-hez SSH-alagút, a Home Assistanthoz
megbízható LAN vagy megfelelően konfigurált HTTPS/VPN. A host tűzfalán a 8883-as
portot csak az MQTT-kliensek hálózatából engedélyezd. Ne továbbítsd az admin- vagy
adatbázisportokat közvetlenül az internet felé. A hoston futó más szolgáltatás is
képes a loopback portok elérésére.

## Első telepítés

Az alábbi lépések új telepítéshez valók. Meglévő adatoknál előbb olvasd el a
„Meglévő telepítés átállítása” részt. Minden parancsot a projekt gyökeréből futtass.

### 1. Titkok

```bash
cp -n .env.example .env
chmod 600 .env
docker run --rm -it --entrypoint node-red nodered/node-red:latest admin hash-pw
openssl rand -hex 32
```

A Node-RED parancsnál adj meg egy egyedi adminjelszót. A kapott bcrypt hash-t
írd a `.env` `NODE_RED_ADMIN_PASSWORD_HASH` változójába **egyszeres idézőjelek
között**, például `NODE_RED_ADMIN_PASSWORD_HASH='$2b$...'`. A teljes hash szükséges;
az idézőjelek megőrzik a dollárjeleket a Compose feldolgozásakor.
Az OpenSSL által generált érték legyen a `NODE_RED_CREDENTIAL_SECRET` (legalább
32 karakter). Ez a flow-k hitelesítő adatainak titkosítási kulcsa; őrizd meg.

A `MYSQL_ROOT_PASSWORD` és `MYSQL_PASSWORD` változókhoz generálj két külön
értéket az `openssl rand -hex 32` paranccsal. A `MYSQL_DATABASE`, `MYSQL_USER`
és `NODE_RED_ADMIN_USER` mintaértékei használhatók. A titkokat ne tedd Gitbe.
Már inicializált MariaDB esetén az `.env` átírása nem cseréli le az adatbázis
felhasználóinak jelszavát: azt külön, SQL-lel kell módosítani.

### 2. Könyvtárak és MQTT TLS

```bash
mkdir -p homeassistant/config mariadb mqtt/data
sudo install -d -m 750 -o 1000 -g 1000 nodered_data
bash scripts/generate-mqtt-tls.sh mqtt.example.lan
sudo chgrp 1883 mqtt/config/tls/server.key
```

A `mqtt.example.lan` helyére a broker tényleges DNS-nevét vagy IPv4-címét írd;
a kliensek ezt a nevet/címet használják. A tanúsítvány a localhost és a
127.0.0.1 elérést is lefedi. A script létrehoz egy saját CA-t és egy 365 napos
szervertanúsítványt, és meglévő TLS-könyvtárat nem ír felül. A `ca.key` privát
kulcsot védd és mentsd; a klienseknek **csak a `ca.crt`** fájlt add át.
Ne kapcsold ki a tanúsítvány- vagy hostname-ellenőrzést.

A broker adatkönyvtárának tulajdonosát az image induláskor beállítja. A
Node-RED `/data` könyvtárát az UID/GID 1000 felhasználónak kell tudnia írni.

### 3. MQTT-felhasználók

```bash
docker run --rm -it --user "$(id -u):$(id -g)" \
  -v "$PWD/mqtt/config:/mosquitto/config" eclipse-mosquitto:latest \
  mosquitto_passwd -c /mosquitto/config/pwfile homeassistant
docker run --rm -it --user "$(id -u):$(id -g)" \
  -v "$PWD/mqtt/config:/mosquitto/config" eclipse-mosquitto:latest \
  mosquitto_passwd /mosquitto/config/pwfile nodered
sudo chgrp 1883 mqtt/config/pwfile
chmod 640 mqtt/config/pwfile
```

A `-c` csak a jelszófájl első létrehozásához kell: meglévő fájlt felülír.
További felhasználókat `-c` nélkül adj hozzá. Minden kliensnek külön jelszót adj.
A két szolgáltatásfiók a teljes topic-fához hozzáfér; csak megbízható automatizálási
szolgáltatásoknak add őket. Például egy `sensor01` fiók saját topicjai
`devices/sensor01/#`, discovery topicjai `homeassistant/<component>/sensor01/#`;
olvashatja a `homeassistant/status` topicot is. Más eszköz topicjaihoz nincs joga.
Az ACL-t a `mqtt/config/acl` fájlban lehet testre szabni.

Felhasználó vagy ACL módosítása után indítsd újra a brokert:
`docker compose restart mosquitto`. Jelszófájl-módosítás után ellenőrizd, hogy
a fájl továbbra is olvasható a 1883-as csoport számára.

### 4. Recorder és indítás

```bash
docker compose config --quiet
python3 scripts/configure-recorder.py
cp -n homeassistant/configuration.yaml.example homeassistant/config/configuration.yaml
docker compose pull
docker compose up -d --wait --wait-timeout 180
docker compose ps
```

A recorder-generátor a Compose interpolációs környezetéből veszi a felhasználót,
jelszót és adatbázisnevet, URL-kódolja őket, és 600-as jogosultságú,
Gitből kizárt `homeassistant/config/recorder.yaml` fájlt készít. Meglévő fájlt
nem ír felül. A `configuration.yaml` fájlban egyetlen recorder-bejegyzés legyen:

```yaml
recorder: !include recorder.yaml
```

A Home Assistant a MariaDB sikeres healthcheckje után indul. A `--wait` a
healthcheck nélküli szolgáltatásoknál csak a futó állapotot igazolja; ezért az
alábbi alkalmazásszintű próbákat is végezd el.

Nyisd meg a `http://<host>:8123` címet, és végezd el az onboardingot. A Home
Assistant MQTT-integrációban állítsd be a broker nevét, a **8883** portot,
a `homeassistant` fiókot, a TLS-t, a CA tanúsítványt és a szervernév-ellenőrzést.
A Node-RED MQTT node-jaiban ugyanezek kellenek a `nodered` fiókkal.

Node-RED távoli használata:

```bash
ssh -N -L 1880:127.0.0.1:1880 felhasznalo@host
```

Ezután `http://127.0.0.1:1880`, belépés a `.env` adminfelhasználójával és a
hash generálásakor megadott jelszóval. A HTTP node-ok is ezt a fiókot kérik;
külső webhookokhoz külön tervezett, védett hozzáférést állíts be.

## Ellenőrzések

```bash
python3 -m unittest discover -s tests -v
docker compose exec mariadb healthcheck.sh --connect --innodb_initialized
curl --fail http://127.0.0.1:1880/auth/login
curl -o /dev/null -w '%{http_code}\n' http://127.0.0.1:1880/flows
openssl s_client -connect 127.0.0.1:8883 -CAfile mqtt/config/tls/ca.crt \
  -verify_return_error -verify_hostname localhost </dev/null
```

Az `/auth/login` hitelesítési sémát adjon vissza; a `/flows` hitelesítés nélküli
lekérése **401** legyen. MQTT klienssel ellenőrizd a TLS-es publish/subscribe-ot,
az anonim és hibás jelszavas kapcsolat elutasítását, illetve hogy egy eszköz
nem írhat más eszköz topicjába. Egy másik LAN-gépről a 3306 és 1880 port ne
legyen elérhető. A Home Assistant felületén a naplókban ne legyen recorder-kapcsolati hiba;
adatbázisban is ellenőrizd, hogy keletkeznek recorder-táblák és új adatok.
Újraindítás után a flow-k, recorder-adatok és MQTT retained üzenetek maradjanak meg.

Az elkülönített automatikus integrációs teszt: `python3 tests/integration.py`.
Ehhez előbb `docker compose pull` szükséges. A teszt saját `/tmp` adatokat és
hálózat nélküli konténereket indít, hostportokat nem publikál, és a végén
eltávolítja a tesztkonténereket és adatokat. Valódi hitelesítést, MQTT ACL-t,
recorder-adatírást, újraindítást és mentésből visszaállítást vizsgál.

## Mentés és visszaállítás

A konzisztens, teljes fájlmentéshez rövid szolgáltatáskiesés szükséges. A futó
MariaDB adatkönyvtárának egyszerű másolása nem megbízható mentés. A titkok és
a Node-RED titkosítási kulcsa is bekerülnek a mentésbe, ezért azt védd, és egy
titkosított, másik gépen tárolt példányt is őrizz meg.

```bash
mkdir -p backups
chmod 700 backups
backup_file="backups/homeassistant-$(date +%Y%m%d-%H%M%S).tar.gz"
docker compose stop
sudo tar --numeric-owner -czf "$backup_file" \
  .env compose.yml mariadb-config nodered-config homeassistant mqtt/config mqtt/data mariadb nodered_data
sudo chmod 600 "$backup_file"
docker compose up -d --wait --wait-timeout 180
```

Ha a mentés hibázik, a szolgáltatásokat akkor is indítsd vissza az utolsó
paranccsal; hibás archívumot ne tekints mentésnek. Jegyezd fel a Git-revíziót és
a `docker compose images` eredményét. Visszaállítást külön tesztgépen vagy az
eredeti stack leállítása után végezz, mert a stack hostportjai ütközhetnek.

Visszaállításkor az elmentett revízióból készíts új checkoutot. Az üres
adatkönyvtárakba `sudo tar --numeric-owner -xzf /abszolut/ut/mentes.tar.gz`
paranccsal állítsd vissza a teljes mentést a projekt gyökeréből, így a
jogosultságok és kulcsok is megmaradnak. Ezután `docker compose config --quiet`,
majd `docker compose up -d --wait --wait-timeout 180` és az alkalmazásszintű
ellenőrzések következnek. Meglévő adatokat visszaállítás előtt külön ments el;
az archívum kicsomagolása felülírhat fájlokat.

## Frissítés és meglévő telepítés átállítása

Az image-ek frissíthető tageket használnak: Home Assistant `stable`, MariaDB,
Mosquitto és Node-RED `latest`. A Compose és az integrációs teszt ugyanazokat
az image-beállításokat használja; nincs verzió- vagy digestpin.

Frissítés előtt készíts teljes mentést, és nézd át a kiadási megjegyzéseket.
Az összes szolgáltatás frissítése:

```bash
docker compose pull
docker compose up -d --wait --wait-timeout 180
```

A `pull` letölti az új image-eket; az `up -d` újralétrehozza az érintett
konténereket. Ezután futtasd a fenti alkalmazásszintű ellenőrzéseket és a
sérülékenységszkennert. A mozgó tagek főverziót is válthatnak. Adatbázis- vagy
HA sémamigráció után visszaállításhoz az előző image-ek és a frissítés előtti
teljes adatmentés szükségesek; jegyezd fel az aktuális image-digesteket a mentéskor.

Meglévő telepítésnél:

1. Mentsd el az összes adatot és az eredeti `nodered_data/settings.js` fájlt.
   A projekt új beállítása ezt a konténerben egy read-only mounttal váltja fel;
   az egyedi node- és runtime-beállításokat emeld át a `nodered-config/settings.js` fájlba.
2. Őrizd meg a korábbi Node-RED `credentialSecret` értéket. Ha eddig rendszer
   generálta, a meglévő runtime konfiguráció kulcsát kell megtartani; új kulcs
   megadásával a régi credentials nem lesznek visszafejthetők. Átállítás előtt
   kövesd a [Node-RED útmutatót](https://nodered.org/docs/getting-started/docker).
3. Az `.env` fájlhoz add hozzá az adminfelhasználót, bcrypt hash-t és a megtartott
   titkosítási kulcsot. Ellenőrizd a `/data` írhatóságát UID 1000-ként.
4. Készíts TLS-t és ACL-t, őrizd meg a meglévő MQTT-jelszófájlt (`-c` nélkül
   módosítsd). Minden MQTT-klienst állíts át TLS-re és az új port/topic-szabályokra.
5. Egyeztesd az aktív adatbázis-hitelesítő adatokat és a recorder URL-jét;
   meglévő HA-konfigurációt ne cserélj le a mintafájllal.
6. Ellenőrizd a jelenlegi MariaDB-verzióról a letöltött verzióra frissítés támogatását,
   teszteld másolaton, majd karbantartási ablakban indítsd újra a stacket.

Tanúsítványmegújításhoz a lejárat előtt ugyanazzal a CA-val írj alá új
szervertanúsítványt, őrizd meg a SAN-neveket és jogosultságokat, majd indítsd újra
a brokert. A generáló script új CA-t készít; teljes újragenerálás esetén minden
kliensnél frissíteni kell a megbízható CA-t is.

## Publikálás és karbantartás

A repository a konfigurációkat, mintafájlokat, telepítési segédscripteket és
teszteket tartalmazza. A helyi `.env`, MQTT-jelszófájlok, TLS-kulcsok,
mentések és futásidejű adatok nem kerülhetnek verziókezelésbe.

A GitHub Actions konfigurációs teszteket, titokkeresést és image-sérülékenységi
ellenőrzést futtat. A mozgó image-tagek miatt a teszteket és a szkennelést
frissítés után ismét el kell végezni; a HIGH/CRITICAL találatokat a CI hibaként
jelzi. A publikálás nem helyettesíti a célgépen végzett működési ellenőrzéseket.

A projekt jelenleg nem tartalmaz licencet.
