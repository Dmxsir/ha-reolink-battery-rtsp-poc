# Reolink Battery RTSP PoC

Experimental Home Assistant integration for researching **on-demand Live View / RTSP** support for Reolink battery cameras such as the **Argus 2E**.

> [!WARNING]
> This repository is a proof of concept. It does **not** provide RTSP yet. The current milestone only runs a bounded Baichuan Live View probe and records secret-safe diagnostics.

## Why a separate integration?

This PoC is intentionally isolated from the main [`ha-reolink-battery`](https://github.com/Dmxsir/ha-reolink-battery) integration.

- Domain: `reolink_battery_rtsp_poc`
- The normal `reolink_battery` integration remains installed and operational.
- The PoC reuses the existing camera UID, local credentials and selected LAN interface from the normal integration; it does not copy the password into its own config entry.
- The PoC shares the main integration's local-operation lock so a recording download and a Live View probe cannot use the battery camera concurrently.
- The Live View transport implementation is private to this PoC and does not monkey-patch the production recording path.

## Current milestone

Pressing **Probe live stream** performs one bounded sequence:

```text
UID/LAN wake -> Baichuan login -> cmd3 mainStream -> sample for 10 seconds -> cmd4 stop -> logout/close
```

The Diagnostics output reports, among other fields:

```text
start_response_code
start_accepted
bcmedia_observed
video_frames
iframe_frames
pframe_frames
h264_frames
h265_frames
total_body_bytes
stop_response_code
stop_accepted
```

The first target is to prove that the camera returns live BcMedia/H264/H265 data. RTSP/go2rtc output will be implemented only after this milestone is verified on hardware.

## Requirements

- Home Assistant with the main **Reolink Battery** custom integration already installed and configured.
- The source camera must have working local credentials and a reachable LAN route as configured by the main integration.
- This PoC currently targets the same legacy local-credential / UID-LAN path used by the main project.

## Install with HACS

1. Open **HACS**.
2. Open the three-dot menu and choose **Custom repositories**.
3. Add:
   `https://github.com/Dmxsir/ha-reolink-battery-rtsp-poc`
4. Select **Integration** as the category.
5. Add the repository and download **Reolink Battery RTSP PoC**.
6. Restart Home Assistant.
7. Go to **Settings -> Devices & services -> Add integration**.
8. Search for **Reolink Battery RTSP PoC**.
9. Select the existing Reolink Battery camera entry.

After setup, open the PoC device and press **Probe live stream** once. Then download Diagnostics from the **Reolink Battery RTSP PoC** integration.

## בעברית

זהו פרויקט ניסיוני נפרד מהאינטגרציה הראשית. אין צורך להסיר או להחליף את `Reolink Battery` הרגילה.

להתקנה דרך HACS: הוסף את `Dmxsir/ha-reolink-battery-rtsp-poc` כ־**Custom repository** מסוג **Integration**, הורד אותו, בצע Restart ל־Home Assistant, ולאחר מכן הוסף את האינטגרציה **Reolink Battery RTSP PoC** ובחר את המצלמה הקיימת.

בשלב הנוכחי הכפתור **בדיקת תצוגה חיה** מעיר את המצלמה, מפעיל `cmd3` למשך 10 שניות, מחפש BcMedia/H264/H265, שולח `cmd4` וסוגר את החיבור. לאחר הבדיקה יש להוציא Diagnostics מה־PoC.

## License

MIT
