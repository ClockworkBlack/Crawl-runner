

def test_a_meeting_system_two_entries_point_at_is_read_once_under_its_own_government():
    """Folkston and Homeland (pages of Charlton County's site) read the county's CivicClerk as their own; Bibb County
    read Macon-Bibb's a second time."""
    from gacrawl.registry import shared_portal_owners

    J = [{"id": "co-charlton", "name": "Charlton County", "kind": "county", "tier": "c", "portals": {"civicclerk": "charltoncoga"}},
         {"id": "folkston", "name": "Folkston", "kind": "city", "tier": "c", "portals": {"civicclerk": "charltoncoga"}},
         {"id": "co-bibb", "name": "Bibb County", "kind": "county", "tier": "a", "portals": {"civicclerk": "maconbibbcoga"}},
         {"id": "macon-bibb", "name": "Macon-Bibb", "kind": "consolidated", "tier": "a", "portals": {"civicclerk": "maconbibbcoga"}},
         {"id": "alpharetta", "name": "Alpharetta", "kind": "city", "tier": "a", "portals": {"civicclerk": "alpharettaga"}}]
    assert shared_portal_owners(J) == {"civicclerk:charltoncoga": "co-charlton", "civicclerk:maconbibbcoga": "macon-bibb"}
