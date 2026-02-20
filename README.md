# BlueLog with Home Assistant (Optional)
## Overview
The BlueLog scans for Bluetooth Low Energy (BLE) devices near your 
and maintains a registry of observed devices with robust presence tracking, fingerprinting, and identity inference.

- Detect nearby BLE devices
- Group devices using a stable fingerprint
- Track presence (enter/leave events)
- Track time while present Daily Presence Aggregation
- Event History
- Classify devices by vendor and beacon type
- Persist structured data for dashboards or analysis
- Send webhook notifications on device entry (HA) 

---

# Core Features

  - Active scanning mode
- Processes:
  - Device address
  - Local name
  - RSSI
  - Manufacturer data
  - Service UUIDs
  - Service data keys

---

### Fingerprint derived from:

- Manufacturer IDs
- Service UUIDs
- Service data keys



# Checking Available Bluetooth Hardware for BLE Visitor Logger

To run the logger, you need to know which Bluetooth adapter to use. This guide shows how to list available adapters and verify they are working.

---

# Instalation
### Create a virtual environment (optional)
```python3 -m venv env
source env/bin/activate
```

### Install Bleak (the only external dependency)
```
pip install bleak
```

## Windows

### Create a virtual environment (optional)
```
python -m venv env
.\env\Scripts\activate
```


### Install Bleak
`pip install bleak`


## MacOS

### Create a virtual environment (optional)
```
python3 -m venv env
source env/bin/activate
```

### Install Bleak
`pip install bleak`



## Run

### List BLE adapters

Use the `hciconfig` or `bluetoothctl` command:


`hciconfig`




hci0:   Type: Primary  Bus: USB
        BD Address: AA:BB:CC:DD:EE:FF  ACL MTU: 310:10  SCO MTU: 64:8
        UP RUNNING
				
				
hci1:   Type: Primary  Bus: USB
        BD Address: 11:22:33:44:55:66  ACL MTU: 310:10  SCO MTU: 64:8
        DOWN



Or using bluetoothctl:

`bluetoothctl list`


Bring an adapter up (if DOWN)
```

sudo hciconfig hci1 up
```


Windows

   Open PowerShell and run:
```

Get-PnpDevice -Class Bluetooth
```

   This lists all Bluetooth adapters.

   Look for Status = OK.



macOS

   Open Terminal and run:

`system_profiler SPBluetoothDataType`

   Look for Bluetooth: Available, Enabled under “Hardware.”

   macOS uses the default system adapter; bleak uses it automatically.



Setting BLE_ADAPTER in the Logger

   Adjust the script:

*BLEADAPTER = "hci1"*  # adjust based on `hciconfig` output

   Windows / macOS:


*BLEADAPTER =* None  # default system adapter

Comment or adjust your HA webhook

*NEWDEVICEWEBHOOKURL =*
*WEBHOOKTIMEOUT = 3*

Then run:

`python3 bluelogger.py`

keep it in background

python2 bluelogger.py &> /dev/null &

start the web server

on the index.html adjust the path of your json log file

python -m http.server 8000


