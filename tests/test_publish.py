"""Publishing a built site to Cloudflare Pages.

Nothing here reaches Cloudflare. The client is a stub, because what matters
is which sites are chosen, what is uploaded, and what is written back --
not whether Cloudflare answers, which only Lyon's machine can prove.
"""

import pytest

from outreach import schema
from outreach.config import load_config
from outreach.providers.cloudflare import CloudflareError, Deployment, site_files
from outreach.slug import site_slug
from outreach.stages import publish

PENANG = "config/campaigns/penang-my.yaml"


def config(tmp_path, project="coai-sites"):
    from dataclasses import replace

    cfg = load_config(PENANG, env_file=None)
    return replace(
        cfg,
        build=replace(
            cfg.build, sites_repo_dir=str(tmp_path), cloudflare_project=project
        ),
        secrets=replace(
            cfg.secrets, cloudflare_api_token="t", cloudflare_account_id="a"
        ),
    )


class FakeCloudflare:
    def __init__(self, fail=()):
        self.published = []
        self._fail = set(fail)

    def publish(self, project, folder):
        if folder.name in self._fail:
            raise CloudflareError("Cloudflare said no")
        self.published.append((project, folder.name))
        return Deployment(
            url=f"https://{folder.name}.{project}.pages.dev",
            deployment_id="d1",
            project=project,
        )


class FakeSheets:
    def __init__(self, leads, briefs):
        self._leads, self._briefs = leads, briefs
        self.column_writes = []

    def read_rows(self, tab):
        return self._leads if tab is schema.LEADS else self._briefs

    def update_column(self, tab, column, values, **k):
        self.column_writes.append((tab, column, dict(values)))
        return len(values)


def make_site(tmp_path, name, lead_id, *, files=("index.html",)):
    folder = tmp_path / site_slug(name, lead_id)
    folder.mkdir(parents=True)
    for f in files:
        (folder / f).write_text("<h1>hi</h1>")
    return folder


def rows(lead_id="a", name="Salon A", approval="Approved", url=""):
    leads = {lead_id: (2, {"Lead ID": lead_id, "Business Name": name})}
    briefs = {
        lead_id: (2, {"Lead ID": lead_id, "Approval Status": approval,
                      "Preview URL": url})
    }
    return leads, briefs


class TestChoosingWhatToPublish:
    def test_an_approved_site_with_a_folder_is_published(self, tmp_path):
        make_site(tmp_path, "Salon A", "a")
        sheets = FakeSheets(*rows())
        cf = FakeCloudflare()

        report = publish.run(config(tmp_path), sheets, cf)

        assert report.published == 1
        # The whole repo, not one business's folder -- see the test below.
        assert cf.published == [("coai-sites", tmp_path.name)]

    def test_the_whole_repo_is_uploaded_not_one_business(self, tmp_path):
        """A Pages deployment is a snapshot of the site, not an addition to
        it. Uploading a single business would produce a live site containing
        only that business, and every page published before it would vanish
        from the main address."""
        make_site(tmp_path, "Salon A", "a")
        make_site(tmp_path, "Salon B", "b")
        leads = {
            "a": (2, {"Lead ID": "a", "Business Name": "Salon A"}),
            "b": (3, {"Lead ID": "b", "Business Name": "Salon B"}),
        }
        briefs = {
            k: (i + 2, {"Lead ID": k, "Approval Status": "Approved",
                        "Preview URL": ""})
            for i, k in enumerate(("a", "b"))
        }
        cf = FakeCloudflare()

        publish.run(config(tmp_path), FakeSheets(leads, briefs), cf)

        assert len(cf.published) == 1, "one upload covers every waiting lead"
        assert cf.published[0][1] == tmp_path.name

    def test_each_business_gets_its_own_path_under_one_project(self, tmp_path):
        make_site(tmp_path, "Salon A", "a")
        sheets = FakeSheets(*rows())

        publish.run(config(tmp_path), sheets, FakeCloudflare())

        _tab, _col, values = sheets.column_writes[0]
        assert values["a"] == (
            f"https://coai-sites.pages.dev/{site_slug('Salon A', 'a')}/"
        )

    def test_a_custom_domain_is_used_when_set(self, tmp_path):
        """So the demo link can be a COAI address rather than pages.dev."""
        from dataclasses import replace

        cfg = config(tmp_path)
        cfg = replace(cfg, build=replace(cfg.build,
                                         public_base_url="https://demos.coai.international"))
        make_site(tmp_path, "Salon A", "a")
        sheets = FakeSheets(*rows())

        publish.run(cfg, sheets, FakeCloudflare())

        _tab, _col, values = sheets.column_writes[0]
        assert values["a"].startswith("https://demos.coai.international/")

    def test_an_unapproved_site_is_left_alone(self, tmp_path):
        make_site(tmp_path, "Salon A", "a")
        sheets = FakeSheets(*rows(approval="Pending"))
        cf = FakeCloudflare()

        report = publish.run(config(tmp_path), sheets, cf)

        assert report.published == 0 and cf.published == []

    def test_a_disqualified_lead_is_never_published(self, tmp_path):
        """The site may already exist from before the owner said stop, but the
        moment a page goes live the outside world can find it. Publish is the
        last gate that can stop it, so it refuses a disqualified lead too."""
        make_site(tmp_path, "Salon A", "a")
        leads = {"a": (2, {"Lead ID": "a", "Business Name": "Salon A",
                           "Stage": "Disqualified"})}
        sheets = FakeSheets(leads, rows()[1])
        cf = FakeCloudflare()

        report = publish.run(config(tmp_path), sheets, cf)

        assert report.disqualified == 1
        assert report.published == 0
        assert cf.published == []
        assert sheets.column_writes == []

    def test_a_site_already_published_is_not_published_again(self, tmp_path):
        """A second publish makes a new address, and the message may already
        have gone out carrying the old one."""
        make_site(tmp_path, "Salon A", "a")
        sheets = FakeSheets(*rows(url="https://old.pages.dev"))
        cf = FakeCloudflare()

        report = publish.run(config(tmp_path), sheets, cf)

        assert report.already_live == 1
        assert cf.published == []

    def test_a_lead_with_no_folder_yet_is_reported_not_failed(self, tmp_path):
        """It has not been pushed from AI Studio yet. That is a waiting
        state, not an error, and the report says which."""
        sheets = FakeSheets(*rows())
        cf = FakeCloudflare()

        report = publish.run(config(tmp_path), sheets, cf)

        assert report.no_folder == 1 and report.failed == 0

    def test_the_url_is_written_back_to_the_sheet(self, tmp_path):
        make_site(tmp_path, "Salon A", "a")
        sheets = FakeSheets(*rows())

        publish.run(config(tmp_path), sheets, FakeCloudflare())

        tab, column, values = sheets.column_writes[0]
        assert tab is schema.SITE_BRIEFS and column == "Preview URL"
        assert values["a"].startswith("https://")

    def test_a_failed_upload_writes_no_urls_at_all(self, tmp_path):
        """One upload serves every waiting lead, so if it fails nobody got a
        page. Recording addresses anyway would put dead links in messages."""
        make_site(tmp_path, "Salon A", "a")
        sheets = FakeSheets(*rows())
        cf = FakeCloudflare(fail={tmp_path.name})

        report = publish.run(config(tmp_path), sheets, cf)

        assert report.failed == 1 and report.published == 0
        assert sheets.column_writes == [], "no URL may be written for a failed publish"


class TestSettingsThatAreNotReadyYet:
    def test_no_repo_folder_says_what_to_set(self, tmp_path):
        from dataclasses import replace

        cfg = replace(config(tmp_path), build=replace(config(tmp_path).build,
                                                      sites_repo_dir=""))
        with pytest.raises(CloudflareError, match="sites_repo_dir"):
            publish.run(cfg, FakeSheets(*rows()), FakeCloudflare())

    def test_a_repo_folder_that_is_not_there_is_named(self, tmp_path):
        from dataclasses import replace

        cfg = config(tmp_path)
        cfg = replace(cfg, build=replace(cfg.build, sites_repo_dir=str(tmp_path / "nope")))
        with pytest.raises(CloudflareError, match="not a folder"):
            publish.run(cfg, FakeSheets(*rows()), FakeCloudflare())

    def test_no_project_points_at_the_check_command(self, tmp_path):
        with pytest.raises(CloudflareError, match="cloudflare-check"):
            publish.run(config(tmp_path, project=""), FakeSheets(*rows()),
                        FakeCloudflare())


class TestWhatGetsUploaded:
    def test_only_web_files_are_sent(self, tmp_path):
        """A repo exported from AI Studio carries source and config that has
        no business on a prospect's website."""
        folder = make_site(tmp_path, "Salon A", "a",
                           files=("index.html", "style.css", "README.md",
                                  "package.json", "logo.png"))
        names = {p.name for p in site_files(folder)}

        assert names == {"index.html", "style.css", "package.json", "logo.png"}
        assert "README.md" not in names

    def test_dotfiles_and_git_are_never_uploaded(self, tmp_path):
        folder = make_site(tmp_path, "Salon A", "a")
        (folder / ".git").mkdir()
        (folder / ".git" / "config").write_text("x")
        (folder / ".env").write_text("SECRET=1")

        names = {p.name for p in site_files(folder)}

        assert names == {"index.html"}, "a .env in a site folder must never ship"

    def test_a_folder_with_no_index_is_refused(self, tmp_path):
        folder = tmp_path / "empty-site"
        folder.mkdir()
        (folder / "style.css").write_text("body{}")

        with pytest.raises(CloudflareError, match="no index.html"):
            site_files(folder)


# ------------------------------------------------- which deployment goes live


class RecordingSession:
    """Stands in for requests, remembering what was posted."""

    def __init__(self, production_branch="main", environment="production"):
        self.production_branch = production_branch
        self.environment = environment
        self.posted = {}

    def get(self, url, **kwargs):
        return FakeResponse({
            "success": True,
            "result": {"production_branch": self.production_branch},
        })

    def post(self, url, **kwargs):
        self.posted = kwargs
        return FakeResponse({
            "success": True,
            "result": {
                "id": "dep-1",
                "url": "https://dep-1.example.pages.dev",
                "environment": self.environment,
            },
        })


class FakeResponse:
    status_code = 200
    ok = True

    def __init__(self, payload):
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def a_site(tmp_path):
    page = tmp_path / "kedai" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text("<h1>Kedai</h1>")
    return tmp_path


def test_publishing_sends_the_projects_production_branch(tmp_path):
    """Without a branch, Cloudflare files a direct upload as a *preview*.

    The upload succeeds, the API returns a URL that works, and the address
    everybody visits goes on serving the previous deployment -- so every
    publish looked like it worked and the live site never changed.
    """
    from outreach.providers.cloudflare import CloudflareClient

    session = RecordingSession(production_branch="main")
    client = CloudflareClient(token="t", account_id="a", session=session)

    deployment = client.publish("coai-sites", a_site(tmp_path))

    sent = dict(session.posted["files"])
    assert "branch" in sent, "no branch means a preview deployment"
    assert sent["branch"][1] == "main"
    assert deployment.is_production is True


def test_the_branch_comes_from_the_project_not_a_guess(tmp_path):
    """A project made from a git repo can use any production branch."""
    from outreach.providers.cloudflare import CloudflareClient

    session = RecordingSession(production_branch="live")
    client = CloudflareClient(token="t", account_id="a", session=session)

    client.publish("coai-sites", a_site(tmp_path))

    assert dict(session.posted["files"])["branch"][1] == "live"


def test_a_deployment_that_landed_as_a_preview_says_so(tmp_path):
    """Reporting it as done would leave a stale site and a happy message."""
    from outreach.providers.cloudflare import CloudflareClient

    session = RecordingSession(environment="preview")
    client = CloudflareClient(token="t", account_id="a", session=session)

    assert client.publish("coai-sites", a_site(tmp_path)).is_production is False


def test_a_preview_deployment_is_reported_as_an_error(tmp_path):
    """Otherwise the Sheet records live URLs that show the old site.

    The upload succeeded, so nothing else in the run looks wrong -- which is
    exactly why it has to be said out loud.
    """
    from outreach.providers.cloudflare import Deployment

    class PreviewClient:
        def publish(self, project, folder):
            return Deployment(
                url="https://dep-2.example.pages.dev",
                deployment_id="dep-2",
                project=project,
                branch="patch-1",
                is_production=False,
            )

    make_site(tmp_path, "Salon A", "a")
    report = publish.run(config(tmp_path), FakeSheets(*rows()), PreviewClient())

    assert report.errors, "a preview deployment was reported as a clean publish"
    assert "PREVIEW" in report.errors[0]
    assert "patch-1" in report.errors[0]
