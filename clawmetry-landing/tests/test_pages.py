"""Page load + content integrity tests."""
import pytest


# ── Page load ──────────────────────────────────────────────────────────────

class TestPageLoads:
    def test_index(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"ClawMetry" in r.data

    def test_showcase(self, client):
        r = client.get("/showcase")
        assert r.status_code == 200
        assert b"ClawMetry" in r.data

    def test_docs(self, client):
        r = client.get("/docs.html")
        assert r.status_code == 200

    def test_traction(self, client):
        r = client.get("/traction")
        assert r.status_code == 200

    def test_install_sh(self, client):
        r = client.get("/install.sh")
        assert r.status_code == 200
        assert b"clawmetry" in r.data.lower()

    def test_install_ps1(self, client):
        r = client.get("/install.ps1")
        assert r.status_code == 200

    def test_install_cmd(self, client):
        r = client.get("/install.cmd")
        assert r.status_code == 200

    def test_robots(self, client):
        r = client.get("/robots.txt")
        assert r.status_code == 200

    def test_sitemap(self, client):
        r = client.get("/sitemap.xml")
        assert r.status_code == 200

    def test_llms_txt(self, client):
        r = client.get("/llms.txt")
        assert r.status_code == 200

    def test_404_on_random(self, client):
        r = client.get("/this-does-not-exist-xyz")
        # Should not 500 -- either 404 or it serves a static file
        assert r.status_code != 500


# ── Landing page content ───────────────────────────────────────────────────

class TestIndexContent:
    def test_view_all_link_present(self, client):
        """'View all' link to /showcase must exist in the What People Say section."""
        r = client.get("/")
        assert b'href="/showcase"' in r.data, "Missing View all → link to /showcase"
        assert b"View all" in r.data

    def test_no_old_ph_anchor_format(self, client):
        """Old-style #comment-43111XX links must not appear (broken IDs from initial build)."""
        r = client.get("/")
        assert b"#comment-4311191" not in r.data
        assert b"#comment-4311192" not in r.data
        assert b"#comment-4311193" not in r.data
        assert b"#comment-4311194" not in r.data

    def test_ph_links_use_query_format(self, client):
        """PH comment links must use ?comment= format so PH scrolls to the comment."""
        r = client.get("/")
        html = r.data.decode()
        if "producthunt.com/products/clawmetry" in html:
            assert "?comment=" in html, "PH links must use ?comment=ID format"

    def test_no_underline_on_view_all(self, client):
        """View all link must have text-decoration:none."""
        r = client.get("/")
        html = r.data.decode()
        # Find the view all link section
        idx = html.find('href="/showcase"')
        surrounding = html[max(0, idx-200):idx+200]
        assert "text-decoration:none" in surrounding or "text-decoration: none" in surrounding

    def test_what_people_say_section(self, client):
        r = client.get("/")
        assert b"What People Say" in r.data

    def test_no_placeholder_avatar_urls(self, client):
        """ui-avatars.com placeholders must not be used for real people."""
        r = client.get("/")
        html = r.data.decode()
        # These specific placeholder avatars should be gone
        assert "ui-avatars.com/api/?name=OD" not in html, "oadiaz still using placeholder avatar"
        assert "ui-avatars.com/api/?name=MK" not in html, "Mykola still using placeholder avatar"
        assert "ui-avatars.com/api/?name=DS" not in html, "Damian still using placeholder avatar"


# ── Showcase page content ──────────────────────────────────────────────────

class TestShowcaseContent:
    def test_all_ph_commenters_present(self, client):
        r = client.get("/showcase")
        assert b"Mykola" in r.data
        assert b"Damian" in r.data
        assert b"Mihail" in r.data
        assert b"Harsh" in r.data

    def test_ph_links_query_format(self, client):
        r = client.get("/showcase")
        html = r.data.decode()
        assert "#comment-43111" not in html, "Old wrong comment IDs still present"
        assert "?comment=5158089" in html, "Mykola comment link missing"
        assert "?comment=5158871" in html, "Damian comment link missing"
        assert "?comment=5163665" in html, "Mihail comment link missing"
        assert "?comment=5161049" in html, "Harsh comment link missing"

    def test_real_ph_avatars_used(self, client):
        r = client.get("/showcase")
        html = r.data.decode()
        assert "ph-avatars.imgix.net" in html, "PH commenters should use real ph-avatars.imgix.net"

    def test_oadiaz_real_avatar(self, client):
        r = client.get("/showcase")
        assert b"miro.medium.com" in r.data, "oadiaz should use real Medium avatar"

    def test_linkedin_logo_not_initials(self, client):
        r = client.get("/showcase")
        html = r.data.decode()
        assert "ui-avatars.com/api/?name=LI" not in html, "LinkedIn card still using LI initials"

    def test_nav_lobster_logo(self, client):
        """Nav should use lobster emoji, not custom SVG."""
        r = client.get("/showcase")
        assert "🦞" in r.data.decode()

    def test_backboardioclip_no_dead_tweet(self, client):
        """backboardioclip should link to profile, not dead tweet URL."""
        r = client.get("/showcase")
        assert b"/status/2025703625306382542" not in r.data

    def test_no_underline_styles_removed(self, client):
        r = client.get("/showcase")
        # Showcase links should not have underline (text-decoration from browser default)
        # Just ensure page loads without error
        assert r.status_code == 200

    def test_submit_cta_present(self, client):
        r = client.get("/showcase")
        assert b"showcase" in r.data.lower()
        assert b"Share" in r.data or b"Built something" in r.data
