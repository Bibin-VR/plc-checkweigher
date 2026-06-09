# Live Logs

Stream live logs from the PLC watcher service (includes plc_reader output):

```bash
journalctl -u plc_watcher -f --no-pager
```

To see both watcher and web server together:

```bash
journalctl -u plc_watcher -u plc_web -f --no-pager
```

To see last 50 lines then follow:

```bash
journalctl -u plc_watcher -n 50 -f --no-pager
```
