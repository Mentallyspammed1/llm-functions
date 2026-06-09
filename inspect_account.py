import sys
sys.path.append(".")
from tools.bybit_terminal import BybitRealm
import json
realm = BybitRealm()
print("--- BALANCE ---")
print(json.dumps(realm.get_wallet_balance(), indent=2))
print("\n--- POSITIONS ---")
print(json.dumps(realm.get_positions(category="linear"), indent=2))
