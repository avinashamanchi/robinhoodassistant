"""Battery work is temporary; AC operation and low-battery release are explicit."""
from datetime import datetime,timedelta,timezone
import pytest

NOW=datetime(2026,11,16,18,tzinfo=timezone.utc)


@pytest.mark.parametrize(("text","source","charge"),[
    ("Now drawing from 'AC Power'\n -InternalBattery-0\t98%; charging;", "ac",98),
    ("Now drawing from 'Battery Power'\n -InternalBattery-0\t93%; discharging;", "battery",93),
    ("unreadable", "unknown",None),
    ("Now drawing from 'Battery Power'\n 150%; discharging;", "unknown",None),
])
def test_power_snapshot_parsing_fails_closed(text,source,charge):
    from trading_assistant.ops.power_guard import parse_power
    assert parse_power(text)=={"source":source,"battery_percent":charge}


def test_battery_authorization_is_explicit_bounded_and_expires():
    from trading_assistant.ops.power_guard import battery_request,guard_decision
    battery={"source":"battery","battery_percent":93}
    assert guard_decision(battery,{},NOW)["active"] is False
    request=battery_request(NOW,120)
    decision=guard_decision(battery,request,NOW)
    assert decision["active"] is True and decision["mode"]=="temporary_battery"
    assert guard_decision(battery,request,NOW+timedelta(minutes=120))["active"] is False
    for minutes in (0,121,True,-1):
        with pytest.raises(ValueError):battery_request(NOW,minutes)


def test_low_battery_unknown_power_and_malformed_requests_do_not_hold_awake():
    from trading_assistant.ops.power_guard import battery_request,guard_decision
    request=battery_request(NOW,120)
    for snapshot in ({"source":"battery","battery_percent":30},
                     {"source":"battery","battery_percent":None},
                     {"source":"unknown","battery_percent":99}):
        assert guard_decision(snapshot,request,NOW)["active"] is False
    for malformed in ({"expires_at":"invalid"},{"issued_at":NOW.isoformat(),"expires_at":(NOW+timedelta(days=2)).isoformat()}):
        assert guard_decision({"source":"battery","battery_percent":90},malformed,NOW)["active"] is False


def test_ac_mode_does_not_require_a_battery_override():
    from trading_assistant.ops.power_guard import guard_decision
    result=guard_decision({"source":"ac","battery_percent":100},{},NOW)
    assert result["active"] is True and result["mode"]=="ac"


def test_controller_releases_assertion_on_low_battery_and_does_not_duplicate_children():
    from trading_assistant.ops.power_guard import GuardController,battery_request
    calls=[]
    class Child:
        pid=123
        dead=False
        def poll(self):return 0 if self.dead else None
        def terminate(self):self.dead=True;calls.append("terminate")
        def wait(self,timeout):return 0
    def spawn(args,**kwargs):calls.append(args);return Child()
    controller=GuardController(spawn=spawn,owner_pid=99)
    request=battery_request(NOW,120)
    assert controller.tick({"source":"battery","battery_percent":90},request,NOW)["guard_active"]
    assert controller.tick({"source":"battery","battery_percent":89},request,NOW)["guard_active"]
    assert len(calls)==1
    assert not controller.tick({"source":"battery","battery_percent":30},request,NOW)["guard_active"]
    assert calls[-1]=="terminate"
    assert "-w" in calls[0] and "99" in calls[0] and "-t" in calls[0]


def test_battery_clock_rollback_cannot_extend_existing_permission():
    from trading_assistant.ops.power_guard import GuardController, battery_request
    elapsed = [0.0]
    children = []
    class Child:
        dead = False
        def poll(self): return 0 if self.dead else None
        def terminate(self): self.dead = True
        def wait(self, timeout): return 0
    def spawn(*args, **kwargs):
        child = Child(); children.append(child); return child
    controller = GuardController(spawn=spawn, monotonic=lambda: elapsed[0])
    request = battery_request(NOW, 1)
    assert controller.tick({"source":"battery", "battery_percent":90}, request, NOW)["guard_active"]
    elapsed[0] = 61
    assert not controller.tick({"source":"battery", "battery_percent":90}, request, NOW)["guard_active"]
    assert len(children) == 1 and children[0].dead
