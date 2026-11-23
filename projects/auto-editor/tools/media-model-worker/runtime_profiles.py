"""Trusted source profile catalog; accepting a definition never approves an artifact."""
import hashlib,json
from pathlib import Path
PHONE_RUNTIME_PROFILE_ID="phone-editorial-v1"
PHONE_RUNTIME_ID="phone-editorial-runtime-1.0.0-py312-darwin-arm64"
FIELDS={"schemaVersion","runtimeProfileId","runtimeId","runtimeVersion","pythonVersion","platform","architecture","minimumOsVersion","dependencyLockSha256","workerDependencyLockSha256","probeModelId","probePromptVersion","probeInputSha256"}
def phone_runtime_profile_definition():
 try:
  root=Path(__file__).resolve().parents[2]/"scripts/runtime-profiles/phone-editorial-v1"
  p=json.loads((root/"profile.json").read_text())
  exact={"schemaVersion":1,"runtimeProfileId":PHONE_RUNTIME_PROFILE_ID,"runtimeId":PHONE_RUNTIME_ID,"runtimeVersion":"1.0.0","pythonVersion":"3.12.14","platform":"darwin","architecture":"arm64","minimumOsVersion":"14.0","probeModelId":"qwen3-vl-2b-instruct-4bit","probePromptVersion":"studio-qwen3-vl-zh-json-v5","probeInputSha256":"1ca0727bf07eb75b2d54a7060fb06b7e86afca54cdb763f47883d292b89b873a"}
  if set(p)!=FIELDS or any(p.get(k)!=v for k,v in exact.items()):return None
  for filename,key in (("dependency.lock.json","dependencyLockSha256"),("worker.lock.json","workerDependencyLockSha256")):
   raw=(root/filename).read_bytes()
   if hashlib.sha256(raw).hexdigest()!=p[key]:return None
   lock=json.loads(raw)
   if lock.get("schemaVersion")!=1 or lock.get("runtimeProfileId")!=p['runtimeProfileId'] or filename=="worker.lock.json" and lock.get("dependencyLockSha256")!=p['dependencyLockSha256']:return None
  return p
 except (OSError,ValueError,TypeError,KeyError):return None

def runtime_profile_for(bundle,legacy_dependency,legacy_worker):
 if "runtimeProfileId" not in bundle:
  return None if bundle.get("id")==PHONE_RUNTIME_ID else {"runtimeProfileId":"legacy","dependencyLockSha256":legacy_dependency,"workerDependencyLockSha256":legacy_worker}
 if bundle.get("runtimeProfileId")!=PHONE_RUNTIME_PROFILE_ID:return None
 p=phone_runtime_profile_definition()
 if p is None or bundle.get("id")!=p['runtimeId'] or bundle.get("version")!=p['runtimeVersion'] or any(bundle.get(k)!=p[k] for k in ("pythonVersion","platform","architecture","minimumOsVersion")):return None
 return p


def runtime_profile_inventory_matches(bundle,inventory):
 if "runtimeProfileId" not in bundle:return True
 p=phone_runtime_profile_definition()
 if p is None or bundle['runtimeProfileId']!=p['runtimeProfileId']:return False
 try:
  import re
  root=Path(__file__).resolve().parents[2]/"scripts/runtime-profiles/phone-editorial-v1"
  graph=json.loads((root/"dependency.lock.json").read_text())
  key=lambda x:re.sub('[-_.]+','-',x['name']).lower()
  expected={key(x):x for x in graph['packages']}
  actual={key(x):x for x in inventory['packages']}
  if len(inventory['packages'])!=len(expected) or actual.keys()!=expected.keys():return False
  if any(any(actual[k].get(f)!=e[f] for f in ('version','sourceSha256','sourceMaterialId')) for k,e in expected.items()):return False
  return all(inventory['python'].get(k)==v for k,v in graph['python'].items())
 except (OSError,ValueError,TypeError,KeyError):return False
