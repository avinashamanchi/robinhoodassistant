"""Alerts use a nonce-paired private destination, fixed text and bounded delivery."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOW=datetime(2026,11,17,tzinfo=timezone.utc)


def module():
    spec=importlib.util.spec_from_file_location("runtime_alerts",Path("scripts/runtime_alerts.py"))
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result


def test_pairing_rejects_strangers_groups_bots_and_expired_nonce(tmp_path):
    m=module()
    pending={"nonce":"a"*32,"issued_at":NOW.isoformat(),"bot_id":42,"username":"test_bot"}
    def update(chat=123,user=123,kind="private",nonce="a"*32,is_bot=False):
        return {"message":{"text":"/start "+nonce,"date":int(NOW.timestamp()),"chat":{"id":chat,"type":kind},"from":{"id":user,"is_bot":is_bot}}}
    for item in (update(user=999),update(kind="group"),update(nonce="b"*32),update(is_bot=True)):
        assert m.match_pairing(pending,[item],NOW) is None
    assert m.match_pairing(pending,[update()],NOW+timedelta(minutes=31)) is None
    assert m.match_pairing(pending,[update()],NOW)["chat_id"]==123
    unicode_message=update();unicode_message["message"]["text"]="hello 👋"
    assert m.match_pairing(pending,[unicode_message,update()],NOW)["chat_id"]==123
    assert m.match_pairing(pending,[None,{"message":None},update()],NOW)["chat_id"]==123


def test_pending_pairing_advances_offset_past_unrelated_backlog(tmp_path):
    m=module()
    from trading_assistant.ops.operational_state import StateStore
    store=StateStore(tmp_path)
    store.update("telegram-pairing",lambda _:{"nonce":"a"*32,"issued_at":NOW.isoformat(),"bot_id":42,"username":"test_bot"})
    offsets=[]
    class Client:
        def call(self,tool,params):
            if tool=="TELEGRAM_GET_ME":return {"id":42,"is_bot":True}
            assert tool=="TELEGRAM_GET_UPDATES"
            offsets.append(params.get("offset",0))
            return [{"update_id":i,"message":{"text":"unrelated"}} for i in range(100)] if len(offsets)==1 else []
    m.deliver(store,Client(),NOW)
    m.deliver(store,Client(),NOW)
    assert offsets==[0,100]


def test_delivery_deduplicates_and_uses_only_paired_recipient(tmp_path):
    m=module()
    from trading_assistant.ops.operational_state import StateStore
    store=StateStore(tmp_path)
    store.update("telegram",lambda _:{"chat_id":123,"bot_id":42,"paired_at":(NOW-timedelta(seconds=1)).isoformat()})
    event={"id":"a"*32,"incident_id":"b"*32,"component":"daemon","code":"heartbeat_stale","state":"open","at":NOW.isoformat(),"chat_id":999}
    store.update("health",lambda _:{"events":[event]})
    sent=[]
    class Client:
        def call(self,tool,params):
            if tool=="TELEGRAM_GET_ME":return {"id":42,"is_bot":True,"username":"test_bot"}
            assert tool=="TELEGRAM_SEND_MESSAGE";sent.append(params)
            return {"message_id":1,"chat":{"id":123}}
    assert m.deliver(store,Client(),NOW)["delivered"]==1
    assert m.deliver(store,Client(),NOW)["delivered"]==0
    assert len(sent)==1 and sent[0]["chat_id"]==123
    assert "heartbeat_stale" in sent[0]["text"] and "999" not in sent[0]["text"]


def test_ambiguous_delivery_is_not_automatically_retried(tmp_path):
    m=module()
    from trading_assistant.ops.operational_state import StateStore
    store=StateStore(tmp_path)
    store.update("telegram",lambda _:{"chat_id":123,"bot_id":42,"paired_at":(NOW-timedelta(seconds=1)).isoformat()})
    store.update("health",lambda _:{"events":[{"id":"a"*32,"incident_id":"b"*32,"component":"daemon","code":"heartbeat_stale","state":"open","at":NOW.isoformat()}]})
    calls=[]
    class Client:
        def call(self,tool,params):
            if tool=="TELEGRAM_GET_ME":return {"id":42,"is_bot":True}
            calls.append(tool);raise m.AlertError("delivery_unavailable")
    assert m.deliver(store,Client(),NOW)["unknown"]==1
    assert m.deliver(store,Client(),NOW+timedelta(minutes=10))["delivered"]==0
    assert len(calls)==1


def test_no_destination_means_no_external_calls(tmp_path):
    m=module()
    from trading_assistant.ops.operational_state import StateStore
    class Client:
        def call(self,*args):raise AssertionError("no paired destination")
    assert m.deliver(StateStore(tmp_path),Client(),NOW)["status"]=="unpaired"


def test_delivery_history_pruning_cannot_resend_events_still_in_health_history(tmp_path):
    m=module()
    from trading_assistant.ops.operational_state import StateStore
    store=StateStore(tmp_path)
    store.update("telegram",lambda _:{"chat_id":123,"bot_id":42,"paired_at":(NOW-timedelta(seconds=1)).isoformat()})
    old={f"{i:032x}":{"state":"delivered"} for i in range(201)}
    events=[{"id":f"{i:032x}","incident_id":"b"*32,"component":"daemon","code":"heartbeat_stale","state":"open","at":NOW.isoformat()} for i in (0,202)]
    store.update("telegram-delivery",lambda _:{"events":old})
    store.update("health",lambda _:{"events":events[::-1]})
    sent=[]
    class Client:
        def call(self,tool,params):
            if tool=="TELEGRAM_GET_ME":return {"id":42,"is_bot":True}
            sent.append(params);return {"message_id":1,"chat":{"id":123}}
    m.deliver(store,Client(),NOW)
    m.deliver(store,Client(),NOW)
    assert len(sent)==1


def test_managed_transport_uses_stdin_and_verifies_telegram_result(tmp_path,monkeypatch):
    import json
    import sys
    m=module()
    executable=tmp_path/"composio"
    executable.write_text(f'''#!{sys.executable}
import json,sys
if sys.argv[1] == 'search':
 print('{{}}')
else:
 assert sys.argv[1:] == ['execute','TELEGRAM_SEND_MESSAGE','--account','trading-alerts','-d','-']
 payload=json.load(sys.stdin)
 assert payload == {{'chat_id':123,'text':'test'}}
 print(json.dumps({{'successful':True,'data':{{'ok':True,'result':{{'message_id':1,'chat':{{'id':123}}}}}}}}))
''')
    executable.chmod(0o700)
    monkeypatch.setattr(m.shutil,"which",lambda _:str(executable))
    result=m.TelegramClient().call("TELEGRAM_SEND_MESSAGE",{"chat_id":123,"text":"test"})
    assert result=={"message_id":1,"chat":{"id":123}}
