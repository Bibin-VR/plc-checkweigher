# Project Status Check

Run a full health check of the PLC check-weigher stack:

```bash
echo "=== PROCESSES ===" && ps aux | grep -E 'plc_|app\.py' | grep -v grep
echo "" && echo "=== SERVICES ===" && systemctl is-active plc_watcher plc_web
echo "" && echo "=== PLC CONNECTIVITY ===" && ping -c 2 -W 1 192.168.3.250 2>&1 | tail -3
echo "" && echo "=== SMB TARGET ===" && ping -c 2 -W 1 192.168.0.140 2>&1 | tail -3
echo "" && echo "=== REPORTS (latest 5) ===" && ls -lt /home/pi/reports/*.pdf 2>/dev/null | head -5
echo "" && echo "=== SMB QUEUE ===" && cat /home/pi/plc_checkweigher/delivery_queue.json 2>/dev/null || echo "Queue empty"
echo "" && echo "=== LIVE STATE ===" && cat /tmp/plc_live.json 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "No live state (reader not running)"
echo "" && echo "=== RECENT LOGS ===" && journalctl -u plc_watcher -n 10 --no-pager
```
