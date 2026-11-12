"""`gacrawl legis video` writes only its JSON to stdout: the workflow keeps stdout in a file and prints a summary from
it, and the progress lines mixed into it (Nov 12) hid the hearing-video step's whole hour from the log and broke that
summary. Progress goes to stderr, where the log shows it as it happens."""
import json

from gacrawl import cli, legis_video


def test_video_progress_goes_to_stderr(tmp_path, monkeypatch, capsys):
    def histories(fetcher, store, deadline=None, log=print, limit=None):
        log("  histories: 3 bills read")
        return {"bills": 3}

    def link_videos(fetcher, store, root, deadline=None, log=print, pages=True):
        log("  channel: 2 recordings")
        return {"linked": 2}

    def hearings(fetcher, store, cfg, deadline=None, log=print):
        log("  hearing 123: captions read")
        return {"transcripts": 1}

    monkeypatch.setattr(legis_video, "histories", histories)
    monkeypatch.setattr(legis_video, "link_videos", link_videos)
    monkeypatch.setattr(legis_video, "hearings", hearings)
    monkeypatch.setattr(cli, "_fetcher", lambda cfg, deadline: None)
    cfg = {"data_dir": str(tmp_path / "data"), "state_dir": tmp_path / "state"}
    cli.cmd_legis(cfg, cli.build_parser().parse_args(["legis", "video", "--with-hearings", "--max-minutes", "1"]))
    got = capsys.readouterr()
    assert json.loads(got.out) == {"histories": {"bills": 3}, "video": {"linked": 2}, "hearings": {"transcripts": 1}}
    assert "histories: 3 bills read" in got.err and "hearing 123" in got.err
