"""Operator-only Telegram incident delivery through managed authentication."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import re
import resource
import secrets
import shutil
import stat
import subprocess
import tempfile

from trading_assistant.installation import require_designated
from trading_assistant.ops.incident_catalog import COMPONENTS, RECOVERY
from trading_assistant.ops.operational_state import StateStore, StateError

ACCOUNT = "trading-alerts"
LIMIT = 1024 * 1024


class AlertError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self):
        found = shutil.which("composio")
        if not found:
            raise AlertError("managed_connection_unavailable")
        path = Path(found).resolve(strict=True)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0,os.getuid()) or info.st_mode & 0o022:
            raise AlertError("managed_connection_unavailable")
        self.executable = str(path)
        self._invoke(["search", "telegram bot profile get updates send message"])

    def _invoke(self,args,params=None):
        def limit_files():
            _,hard = resource.getrlimit(resource.RLIMIT_FSIZE)
            maximum = LIMIT if hard == resource.RLIM_INFINITY else min(hard,LIMIT)
            resource.setrlimit(resource.RLIMIT_FSIZE,(maximum,maximum))
        try:
            with tempfile.TemporaryFile() as request, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                if params is not None:
                    request.write(json.dumps(params,allow_nan=False).encode());request.seek(0)
                completed = subprocess.run([self.executable,*args],stdin=request,stdout=output,stderr=errors,
                                           timeout=40,preexec_fn=limit_files,
                                           env={"HOME":str(Path.home()),"PATH":"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin","LANG":"en_US.UTF-8"})
                if completed.returncode or any(os.fstat(h.fileno()).st_size >= LIMIT for h in (output,errors)):
                    raise AlertError("delivery_unavailable")
                output.seek(0);raw=output.read(LIMIT+1)
                payload=json.loads(raw)
            if isinstance(payload,dict) and payload.get("storedInFile") is True:
                fd=os.open(payload["outputFilePath"],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                with os.fdopen(fd,"rb") as source:
                    info=os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > LIMIT:
                        raise AlertError("delivery_unavailable")
                    raw=source.read(LIMIT+1)
                if len(raw)>LIMIT:raise AlertError("delivery_unavailable")
                payload=json.loads(raw)
            if not isinstance(payload,dict):raise AlertError("delivery_unavailable")
            return payload
        except (OSError,subprocess.SubprocessError,ValueError,KeyError,TypeError):
            raise AlertError("delivery_unavailable") from None

    def call(self,tool,params):
        if tool not in {"TELEGRAM_GET_ME","TELEGRAM_GET_UPDATES","TELEGRAM_SEND_MESSAGE"}:
            raise AlertError("tool_not_allowed")
        payload=self._invoke(["execute",tool,"--account",ACCOUNT,"-d","-"],params)
        data=payload.get("data")
        if payload.get("successful") is not True or not isinstance(data,dict) or data.get("ok") is not True or "result" not in data:
            raise AlertError("delivery_unavailable")
        return data["result"]


def _date(value):
    parsed=datetime.fromisoformat(value)
    if parsed.utcoffset() is None:raise ValueError("aware timestamp required")
    return parsed.astimezone(timezone.utc)


def _profile(client):
    profile=client.call("TELEGRAM_GET_ME",{})
    if not isinstance(profile,dict) or profile.get("is_bot") is not True or type(profile.get("id")) is not int:
        raise AlertError("bot_identity_unverified")
    return profile


def match_pairing(pending,updates,now):
    try:
        issued=_date(pending["issued_at"])
        nonce=pending["nonce"]
        if not isinstance(nonce,str) or not re.fullmatch(r"[a-f0-9]{32}",nonce) or not 0 <= (now-issued).total_seconds() <= 1800:
            return None
        for update in updates:
            if not isinstance(update,dict) or not isinstance(update.get("message"),dict):
                continue
            message=update.get("message",{})
            chat=message.get("chat",{});sender=message.get("from",{})
            if not isinstance(chat,dict) or not isinstance(sender,dict):
                continue
            text=message.get("text","")
            if (chat.get("type")=="private" and type(chat.get("id")) is int and chat["id"]>0
                    and sender.get("id")==chat["id"] and sender.get("is_bot") is False
                    and isinstance(text,str) and hmac.compare_digest(text.encode("utf-8"),("/start "+nonce).encode("ascii"))
                    and type(message.get("date")) is int and issued.timestamp()-5 <= message["date"] <= now.timestamp()+5):
                return {"chat_id":chat["id"],"bot_id":pending["bot_id"],"paired_at":now.isoformat()}
    except (ValueError,KeyError,TypeError,AttributeError):
        return None
    return None


def begin_pairing(store,client,now):
    profile=_profile(client)
    username=profile.get("username")
    if not isinstance(username,str) or not re.fullmatch(r"[A-Za-z0-9_]{5,32}",username):
        raise AlertError("bot_identity_unverified")
    pending={"nonce":secrets.token_hex(16),"issued_at":now.isoformat(),"bot_id":profile["id"],"username":username}
    store.update("telegram-pairing",lambda _:pending)
    return {"status":"pairing_pending","start_url":f"https://t.me/{username}?start={pending['nonce']}"}


def event_text(event):
    component=event.get("component");code=event.get("code")
    if (component not in COMPONENTS or code not in COMPONENTS[component]
            or event.get("state") not in {"open","resolved"}
            or not re.fullmatch(r"[a-f0-9]{32}",event.get("id",""))):
        raise AlertError("invalid_incident")
    at=_date(event["at"]).isoformat()
    return f"Trading Assistant PAPER: {event['state']}\n{component}: {code}\n{at}\n{RECOVERY[component]}"


def deliver(store,client,now):
    with store.lease("alerts-owner"):
        destination=store.load("telegram")
        if not destination:
            pending=store.load("telegram-pairing")
            if not pending:return {"status":"unpaired","delivered":0}
            if not 0 <= (now-_date(pending["issued_at"])).total_seconds() <= 1800:
                store.update("telegram-pairing",lambda _:{})
                return {"status":"pairing_expired","delivered":0}
            profile=_profile(client)
            if profile["id"] != pending.get("bot_id"):raise AlertError("bot_identity_changed")
            params={"limit":100,"timeout":0,"allowed_updates":["message"]}
            offset=pending.get("offset")
            if type(offset) is int and offset>=0:params["offset"]=offset
            updates=client.call("TELEGRAM_GET_UPDATES",params)
            if not isinstance(updates,list):raise AlertError("pairing_unavailable")
            destination=match_pairing(pending,updates,now)
            if not destination:
                identifiers=[u["update_id"] for u in updates if isinstance(u,dict) and type(u.get("update_id")) is int and u["update_id"]>=0]
                if identifiers:
                    store.update("telegram-pairing",lambda old:{**old,"offset":max(identifiers)+1})
                return {"status":"pairing_pending","delivered":0}
            # Persist the destination before the test send; an uncertain send is never repeated.
            store.update("telegram",lambda _:destination)
            store.update("telegram-pairing",lambda _:{})
            result=client.call("TELEGRAM_SEND_MESSAGE",{"chat_id":destination["chat_id"],"text":"Trading Assistant PAPER: Telegram incident alerts paired. This is the setup test alert."})
            if not isinstance(result,dict) or result.get("chat",{}).get("id") != destination["chat_id"] or type(result.get("message_id")) is not int:
                raise AlertError("test_delivery_unconfirmed")
            store.update("telegram",lambda old:{**old,"test_delivered_at":now.isoformat()})
        if type(destination.get("chat_id")) is not int or destination["chat_id"]<=0:
            raise AlertError("destination_invalid")
        profile=_profile(client)
        if profile["id"] != destination.get("bot_id"):raise AlertError("bot_identity_changed")
        events=store.load("health").get("events",[])
        retained={event.get("id") for event in events}
        sent={key:value for key,value in store.load("telegram-delivery").get("events",{}).items() if key in retained}
        delivered=unknown=0
        for event in events:
            text=event_text(event)
            if event["id"] in sent or _date(event["at"]) < _date(destination["paired_at"]):continue
            # Claim durably before network I/O. Process death/timeout is ambiguous, not permission to resend.
            sent[event["id"]]={"state":"unknown","attempted_at":now.isoformat()}
            store.update("telegram-delivery",lambda _:{"events":sent})
            try:
                result=client.call("TELEGRAM_SEND_MESSAGE",{"chat_id":destination["chat_id"],"text":text})
                if not isinstance(result,dict) or result.get("chat",{}).get("id") != destination["chat_id"] or type(result.get("message_id")) is not int:
                    raise AlertError("delivery_unconfirmed")
                sent[event["id"]].update(state="delivered",message_id=result["message_id"])
                delivered+=1
            except AlertError:
                unknown+=1
            store.update("telegram-delivery",lambda _:{"events":sent})
            if delivered+unknown>=5:break
        return {"status":"paired","delivered":delivered,"unknown":unknown}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("pair","deliver","status"))
    args=parser.parse_args(argv)
    root=Path(__file__).resolve().parents[1]
    try:
        require_designated(root);store=StateStore(root);now=datetime.now(timezone.utc)
        if args.command=="status":
            destination=store.load("telegram")
            result={"paired":bool(destination),"test_confirmed":bool(destination.get("test_delivered_at"))}
        elif args.command=="deliver" and not store.load("telegram") and not store.load("telegram-pairing"):
            result={"status":"unpaired","delivered":0}
        else:
            client=TelegramClient()
            result=begin_pairing(store,client,now) if args.command=="pair" else deliver(store,client,now)
        if args.command != "status":
            store.update("telegram-status",lambda _:{"checked_at":now.isoformat(),"status":"available"})
        print(json.dumps(result,sort_keys=True));return 0
    except (AlertError,StateError,OSError,ValueError):
        try:
            store.update("telegram-status",lambda _:{"checked_at":now.isoformat(),"status":"unavailable"})
        except (UnboundLocalError,StateError,OSError):
            pass
        print('{"status":"unavailable","code":"telegram_setup_or_delivery_failed"}')
        return 1


if __name__=="__main__":
    raise SystemExit(main())
