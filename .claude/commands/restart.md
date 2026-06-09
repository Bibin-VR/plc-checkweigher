# Restart Services

Restart both PLC stack services:

```bash
sudo systemctl restart plc_watcher plc_web
sleep 2
systemctl status plc_watcher plc_web --no-pager | grep -E 'Active|PID'
```

Restart only the watcher (stops any active plc_reader session):

```bash
sudo systemctl restart plc_watcher
```

Restart only the web server:

```bash
sudo systemctl restart plc_web
```
