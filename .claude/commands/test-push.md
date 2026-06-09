# Test SMB Push

Test the full SMB push pipeline: generate a live PDF from PLC, push it to the Windows share, verify delivery.

```bash
cd /home/pi/plc_checkweigher

echo "=== Generating test PDF from PLC ===" 
/home/pi/plc_env/bin/python3 plc_report.py 2>&1

echo "" && echo "=== Latest PDF ===" 
LATEST=$(ls -t /home/pi/reports/*.pdf 2>/dev/null | head -1)
echo "$LATEST"

echo "" && echo "=== Pushing to SMB ===" 
/home/pi/plc_env/bin/python3 -c "
from pdf_push import _push_smb, _already_sent
import os
path = '$LATEST'
fname = os.path.basename(path)
if _already_sent(fname):
    print(f'  Already in ledger: {fname}')
else:
    _push_smb(path)
"

echo "" && echo "=== Delivery ledger ===" 
cat /home/pi/plc_checkweigher/delivery_sent.log 2>/dev/null || echo "(empty)"

echo "" && echo "=== Pending queue ===" 
cat /home/pi/plc_checkweigher/delivery_queue.json 2>/dev/null || echo "(empty)"
```
