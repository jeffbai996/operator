"""The one decision that picks the streamed tab (2026-09-22, polish run).

Three mechanisms used to compete: a url-diff mover, a document.visibilityState
fallback that every tab on this Chrome answers 'visible' to, and a tab-count
switch that fired mid-run. They are now one table: given what the streamer
knows, which tab does it show, and why.
"""
import operator_view as OV


class P:
    def __init__(self, name, url="https://example.com/" + "x"):
        self.name, self.url = name, url

    def __repr__(self):
        return f"P({self.name})"


def decide(**kw):
    base = dict(live=[], current=None, busy=False, owned=None, front=None,
                movers=[], count_changed=False)
    base.update(kw)
    return OV.decide_streamed_tab(**base)


def test_no_live_pages_means_nothing_to_show():
    assert decide() == (None, "")


def test_a_dead_current_falls_to_the_newest_page_with_content():
    a, b, blank = P("a"), P("b"), P("blank", "chrome://new-tab-page/")
    assert decide(live=[a, b, blank], current=None) == (b, "dead-current")


def test_the_owned_tab_wins_over_front_and_movers_while_busy():
    cur, owned, front = P("cur"), P("owned"), P("front")
    out = decide(live=[cur, owned, front], current=cur, busy=True,
                 owned=owned, front=front, movers=[front])
    assert out == (owned, "owned")


def test_already_on_the_owned_tab_never_switches_away():
    owned, front = P("owned"), P("front")
    out = decide(live=[owned, front], current=owned, busy=True,
                 owned=owned, front=front, movers=[front], count_changed=True)
    assert out == (None, "")


def test_without_a_registry_entry_the_agents_activity_is_followed():
    cur, mover = P("cur"), P("mover")
    assert decide(live=[cur, mover], current=cur, busy=True,
                  movers=[mover]) == (mover, "mover")


def test_busy_with_no_owner_or_activity_follows_the_front_tab():
    cur, front = P("cur"), P("front")
    assert decide(live=[cur, front], current=cur, busy=True,
                  front=front) == (front, "front")


def test_idle_follows_the_humans_front_tab_but_never_an_empty_one():
    cur, front, ntp = P("cur"), P("front"), P("ntp", "chrome://newtab/")
    assert decide(live=[cur, front], current=cur, front=front) == (front, "front")
    assert decide(live=[cur, ntp], current=cur, front=ntp) == (None, "")


def test_idle_new_tab_is_shown_when_nothing_else_says_otherwise():
    cur, new = P("cur"), P("new")
    assert decide(live=[cur, new], current=cur, count_changed=True) == (new, "new-tab")


def test_a_new_tab_never_yanks_the_view_mid_run():
    cur, new = P("cur"), P("new")
    assert decide(live=[cur, new], current=cur, busy=True,
                  count_changed=True) == (None, "")


def test_nothing_changed_keeps_the_current_tab():
    cur, other = P("cur"), P("other")
    assert decide(live=[cur, other], current=cur, front=cur) == (None, "")
