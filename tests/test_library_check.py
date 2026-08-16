from __future__ import annotations

import json
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from compatibility.helpers.epub_factory import create_epub
from kavita_ingest.cli import app
from kavita_ingest.config import AppConfig
from kavita_ingest.library_check import check_library, check_library_roots


def _comic(
    path: Path,
    *,
    series: str,
    number: str = "1",
    volume: str = "",
    title: str = "Chapter One",
    format_: str = "",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        f"<Title>{title}</Title>",
        f"<Series>{series}</Series>",
    ]
    if number:
        fields.append(f"<Number>{number}</Number>")
    if volume:
        fields.append(f"<Volume>{volume}</Volume>")
    if format_:
        fields.append(f"<Format>{format_}</Format>")
    xml = (
        "<?xml version='1.0' encoding='utf-8'?><ComicInfo>" + "".join(fields) + "</ComicInfo>"
    ).encode()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", b"image")
        archive.writestr("ComicInfo.xml", xml)
    return path


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        books_root=tmp_path / "Libraries" / "Kavita" / "Books",
        comics_root=tmp_path / "Libraries" / "Kavita" / "Comics",
        database_path=tmp_path / "state.sqlite3",
    )


def test_library_check_accepts_configured_parent_and_canonical_media(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.books_root is not None
    assert config.comics_root is not None
    book = config.books_root / "Fixture Series" / "Fixture Series - 1.5 - Fixture Book.epub"
    book.parent.mkdir(parents=True)
    create_epub(book)
    comic = config.comics_root / "Series (2024)" / "Series (2024) - 001 - Chapter One.cbz"
    _comic(comic, series="Series (2024)")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (book, comic)}

    result = check_library(tmp_path / "Libraries", config)

    assert result.kavita_ready is True
    assert result.canonical is True
    assert len(result.media) == 2
    assert result.findings == ()
    assert {scope.kind for scope in result.scopes} == {"books", "comics"}
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before} == before


def test_library_check_accepts_multiple_explicit_roots_without_duplicate_scans(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.books_root is not None
    assert config.comics_root is not None
    book = config.books_root / "Fixture Series" / "Fixture Series - 1.5 - Fixture Book.epub"
    book.parent.mkdir(parents=True)
    create_epub(book)
    comic = config.comics_root / "Series (2024)" / "Series (2024) - 001 - Chapter One.cbz"
    _comic(comic, series="Series (2024)")

    result = check_library_roots(
        (config.books_root, config.comics_root, config.comics_root / "Series (2024)"),
        config,
    )

    assert result.kavita_ready is True
    assert result.canonical is True
    assert len(result.media) == 2
    assert {scope.kind for scope in result.scopes} == {"books", "comics"}
    assert len(result.scopes) == 2


def test_library_check_flags_root_level_media_as_readiness_error(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.comics_root is not None
    config.comics_root.mkdir(parents=True)
    comic = config.comics_root / "Series (2024) - 001 - Chapter One.cbz"
    _comic(comic, series="Series (2024)")

    result = check_library(config.comics_root, config)

    codes = {finding.code for finding in result.findings}
    assert "media_at_library_root" in codes
    assert result.kavita_ready is False


def test_library_check_reports_expected_path_for_metadata_folder_drift(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.comics_root is not None
    comic = config.comics_root / "Wrong Folder" / "Wrong Name.cbz"
    _comic(comic, series="Series (2024)")

    result = check_library(config.comics_root, config)

    by_code = {finding.code: finding for finding in result.findings}
    assert by_code["comic_series_folder_mismatch"].severity == "error"
    assert by_code["comic_series_folder_mismatch"].expected == "Series (2024)"
    assert by_code["noncanonical_comic_path"].expected == (
        "Series (2024)/Series (2024) - 001 - Chapter One.cbz"
    )


def test_library_check_accepts_canonical_collection_volume(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.comics_root is not None
    comic = config.comics_root / "Saga" / "Specials" / "Saga - v01 - Saga Vol. 1.cbz"
    _comic(
        comic,
        series="Saga",
        number="",
        volume="1",
        title="Saga Vol. 1",
        format_="Trade Paperback",
    )

    result = check_library(config.comics_root, config)

    assert result.kavita_ready is True
    assert result.canonical is True
    assert result.findings == ()


def test_library_check_places_comicinfo_specials_under_specials(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.comics_root is not None
    comic = config.comics_root / "Saga" / "Saga - v01 - Saga Vol. 1.cbz"
    _comic(comic, series="Saga", number="", title="Saga Vol. 1", format_="Trade Paperback")

    # The file is readable by Kavita, but it drifts from kavita-ingest's configured Specials layout.
    result = check_library(config.comics_root, config)

    finding = next(item for item in result.findings if item.code == "noncanonical_comic_folder")
    assert finding.expected == "Saga/Specials"
    assert finding.severity == "warning"
    assert result.kavita_ready is True
    assert result.canonical is False


def test_library_check_detects_duplicate_comic_slot(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.comics_root is not None
    first = config.comics_root / "Series (2024)" / "Series (2024) - 001 - A.cbz"
    second = config.comics_root / "Series (2024)" / "Series (2024) - 001 - B.cbz"
    _comic(first, series="Series (2024)", title="A")
    _comic(second, series="Series (2024)", title="B")

    result = check_library(config.comics_root, config)

    duplicates = [item for item in result.findings if item.code == "duplicate_comic_slot"]
    assert len(duplicates) == 2
    assert result.kavita_ready is False


def test_library_check_flags_legacy_collection_number_semantics(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.comics_root is not None
    comic = config.comics_root / "Saga" / "Specials" / "Saga - 001 - Saga Vol. 1.cbz"
    _comic(
        comic,
        series="Saga",
        number="1",
        title="Saga Vol. 1",
        format_="Trade Paperback",
    )

    result = check_library(config.comics_root, config)

    by_code = {item.code: item for item in result.findings}
    assert by_code["collection_format_uses_issue_number"].severity == "warning"
    assert by_code["noncanonical_comic_path"].expected == (
        "Saga/Specials/Saga - v01 - Saga Vol. 1.cbz"
    )


def test_library_check_flags_bare_edition_label_title(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.comics_root is not None
    comic = (
        config.comics_root
        / "Spider-Man - Life Story"
        / "Specials"
        / "Spider-Man - Life Story - 2nd edition.cbz"
    )
    _comic(
        comic,
        series="Spider-Man - Life Story",
        number="",
        title="2nd edition",
        format_="Trade Paperback",
    )

    result = check_library(config.comics_root, config)

    finding = next(
        item for item in result.findings if item.code == "comic_title_is_only_edition_label"
    )
    assert finding.severity == "warning"
    assert result.kavita_ready is True
    assert result.canonical is False


def test_library_check_cli_json_and_strict_exit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.comics_root is not None
    comic = config.comics_root / "Series (2024)" / "Odd.cbz"
    _comic(comic, series="Series (2024)")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[paths]\nbooks = "{config.books_root}"\ncomics = "{config.comics_root}"\n',
        encoding="utf-8",
    )
    runner = CliRunner()

    human = runner.invoke(
        app, ["library-check", str(config.comics_root), "--config", str(config_path)]
    )
    strict = runner.invoke(
        app,
        ["library-check", str(config.comics_root), "--config", str(config_path), "--strict"],
    )
    machine = runner.invoke(
        app,
        ["library-check", str(config.comics_root), "--config", str(config_path), "--json"],
    )

    assert human.exit_code == 0, human.output
    assert "Kavita readiness: PASS" in human.output
    assert "CLEANUP RECOMMENDED" in human.output
    assert "Read-only check:" in human.output
    assert strict.exit_code == 1
    assert machine.exit_code == 0
    payload = json.loads(machine.output)
    assert payload["command"] == "library-check"
    assert payload["summary"]["kavita_ready"] is True
    assert payload["summary"]["canonical"] is False


def test_library_check_human_output_is_grouped_plain_and_colour_capable(tmp_path: Path) -> None:
    import io

    from rich.console import Console

    from kavita_ingest.library_check import render_library_check

    config = _config(tmp_path)
    assert config.comics_root is not None
    comic = config.comics_root / "Saga" / "Specials" / "Saga - 001 - Saga Vol. 1.cbz"
    _comic(
        comic,
        series="Saga",
        number="1",
        title="Saga Vol. 1",
        format_="Trade Paperback",
    )
    result = check_library(config.comics_root, config)
    plain_stream = io.StringIO()
    render_library_check(result, Console(file=plain_stream, width=120))
    rendered = plain_stream.getvalue()
    assert "Kavita readiness: PASS" in rendered
    assert "CLEANUP RECOMMENDED" in rendered
    assert "2 warnings across 1 file" in rendered
    assert "Collected edition uses issue Number instead of Volume" in rendered
    assert "Recommended:" in rendered
    assert "Diagnostic code:" not in rendered

    colour_stream = io.StringIO()
    colour_console = Console(
        file=colour_stream, force_terminal=True, color_system="standard", width=120
    )
    render_library_check(result, colour_console)
    assert "\x1b[" in colour_stream.getvalue()


def test_library_check_clean_human_output_has_green_pass_message(tmp_path: Path) -> None:
    import io

    from rich.console import Console

    from kavita_ingest.library_check import render_library_check

    config = _config(tmp_path)
    assert config.comics_root is not None
    comic = config.comics_root / "Saga" / "Specials" / "Saga - v01 - Saga Vol. 1.cbz"
    _comic(
        comic,
        series="Saga",
        number="",
        volume="1",
        title="Saga Vol. 1",
        format_="Trade Paperback",
    )
    result = check_library(config.comics_root, config)
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, color_system="standard", width=120)

    render_library_check(result, console)

    rendered = stream.getvalue()
    assert "Kavita readiness: PASS" in rendered
    assert "kavita-ingest layout: CANONICAL" in rendered
    assert "No problems found" in rendered
    assert "\x1b[" in rendered


def test_library_repair_plan_separates_safe_repairs_from_human_judgement(tmp_path: Path) -> None:
    from kavita_ingest.library_repair import plan_library_repairs

    config = _config(tmp_path)
    assert config.comics_root is not None
    legacy = config.comics_root / "Saga" / "Specials" / "Saga - 001 - Saga Vol. 1.cbz"
    _comic(
        legacy,
        series="Saga",
        number="1",
        title="Saga Vol. 1",
        format_="Trade Paperback",
    )
    ambiguous = (
        config.comics_root
        / "Spider-Man - Life Story"
        / "Specials"
        / "Spider-Man - Life Story - 2nd edition.cbz"
    )
    _comic(
        ambiguous,
        series="Spider-Man - Life Story",
        number="",
        title="2nd edition",
        format_="Trade Paperback",
    )

    result = check_library(config.comics_root, config)
    plan = plan_library_repairs(result, config)

    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.source == legacy
    assert action.destination.name == "Saga - v01 - Saga Vol. 1.cbz"
    assert action.kind == "rewrite_cbz"
    assert dict(action.set_fields) == {"Volume": "1"}
    assert action.clear_fields == ("Number",)
    assert len(plan.manual) == 1
    assert plan.manual[0].path == ambiguous
    assert any("human decision" in line for line in plan.manual[0].guidance)


def test_library_repair_applies_legacy_collection_migration_and_rechecks_clean(
    tmp_path: Path,
) -> None:
    import zipfile

    from kavita_ingest.library_repair import apply_library_repairs, plan_library_repairs

    config = _config(tmp_path)
    assert config.comics_root is not None
    legacy = config.comics_root / "Saga" / "Specials" / "Saga - 001 - Saga Vol. 1.cbz"
    _comic(
        legacy,
        series="Saga",
        number="1",
        title="Saga Vol. 1",
        format_="Trade Paperback",
    )
    expected = config.comics_root / "Saga" / "Specials" / "Saga - v01 - Saga Vol. 1.cbz"

    result = check_library(config.comics_root, config)
    plan = plan_library_repairs(result, config)
    outcome = apply_library_repairs(plan, config)

    assert len(outcome.completed) == 1
    assert not legacy.exists()
    assert expected.is_file()
    with zipfile.ZipFile(expected) as archive:
        xml = archive.read("ComicInfo.xml").decode("utf-8")
    assert "<Volume>1</Volume>" in xml
    assert "<Number>" not in xml
    refreshed = check_library(config.comics_root, config)
    assert refreshed.canonical is True
    assert refreshed.findings == ()


def test_library_repair_moves_noncanonical_book_without_rewriting_bytes(tmp_path: Path) -> None:
    from kavita_ingest.filesystem import sha256_file
    from kavita_ingest.library_repair import apply_library_repairs, plan_library_repairs

    config = _config(tmp_path)
    assert config.books_root is not None
    canonical = config.books_root / "Fixture Series" / "Fixture Series - 1.5 - Fixture Book.epub"
    canonical.parent.mkdir(parents=True)
    create_epub(canonical)
    wrong = config.books_root / "Wrong Folder" / "Wrong Name.epub"
    wrong.parent.mkdir(parents=True)
    canonical.rename(wrong)
    original_hash = sha256_file(wrong)

    result = check_library(config.books_root, config)
    plan = plan_library_repairs(result, config)

    assert len(plan.actions) == 1
    assert plan.actions[0].kind == "move"
    assert plan.actions[0].destination == canonical
    apply_library_repairs(plan, config)
    assert canonical.is_file()
    assert sha256_file(canonical) == original_hash
    assert not wrong.exists()


def test_library_repair_refuses_destination_collision(tmp_path: Path) -> None:
    from kavita_ingest.library_repair import plan_library_repairs

    config = _config(tmp_path)
    assert config.comics_root is not None
    legacy = config.comics_root / "Saga" / "Specials" / "Saga - 001 - Saga Vol. 1.cbz"
    expected = config.comics_root / "Saga" / "Specials" / "Saga - v01 - Saga Vol. 1.cbz"
    _comic(
        legacy,
        series="Saga",
        number="1",
        title="Saga Vol. 1",
        format_="Trade Paperback",
    )
    _comic(
        expected,
        series="Saga",
        number="",
        volume="1",
        title="Saga Vol. 1",
        format_="Trade Paperback",
    )

    plan = plan_library_repairs(check_library(config.comics_root, config), config)

    assert plan.actions == ()
    assert any(advice.path == legacy for advice in plan.manual)
    assert any(
        "duplicate" in line.casefold()
        for advice in plan.manual
        for line in advice.guidance
    )


def test_library_repair_refuses_source_changed_after_plan(tmp_path: Path) -> None:
    import pytest

    from kavita_ingest.library_repair import apply_library_repairs, plan_library_repairs

    config = _config(tmp_path)
    assert config.comics_root is not None
    legacy = config.comics_root / "Saga" / "Specials" / "Saga - 001 - Saga Vol. 1.cbz"
    _comic(
        legacy,
        series="Saga",
        number="1",
        title="Saga Vol. 1",
        format_="Trade Paperback",
    )
    plan = plan_library_repairs(check_library(config.comics_root, config), config)
    legacy.write_bytes(legacy.read_bytes() + b"changed")

    with pytest.raises(ValueError, match="changed since the plan"):
        apply_library_repairs(plan, config)


def test_library_fix_cli_applies_safe_repairs_and_leaves_manual_issue(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.comics_root is not None
    legacy = config.comics_root / "Saga" / "Specials" / "Saga - 001 - Saga Vol. 1.cbz"
    _comic(
        legacy,
        series="Saga",
        number="1",
        title="Saga Vol. 1",
        format_="Trade Paperback",
    )
    ambiguous = (
        config.comics_root
        / "Spider-Man - Life Story"
        / "Specials"
        / "Spider-Man - Life Story - 2nd edition.cbz"
    )
    _comic(
        ambiguous,
        series="Spider-Man - Life Story",
        number="",
        title="2nd edition",
        format_="Trade Paperback",
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[paths]\nbooks = "{config.books_root}"\ncomics = "{config.comics_root}"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["library-fix", str(config.comics_root), "--config", str(config_path), "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert "Applied 1 safe repair" in result.output
    assert "Comic title contains only an edition label" in result.output
    assert (config.comics_root / "Saga" / "Specials" / "Saga - v01 - Saga Vol. 1.cbz").is_file()
    assert ambiguous.is_file()


def test_library_repair_current_drift_shape_offers_six_safe_and_one_manual(tmp_path: Path) -> None:
    from kavita_ingest.library_repair import plan_library_repairs

    config = _config(tmp_path)
    assert config.books_root is not None
    assert config.comics_root is not None

    canonical_book = (
        config.books_root
        / "Fixture Series"
        / "Fixture Series - 1.5 - Fixture Book.epub"
    )
    canonical_book.parent.mkdir(parents=True)
    create_epub(canonical_book)
    wrong_book = config.books_root / "Old Fixture" / "Old Fixture.epub"
    wrong_book.parent.mkdir(parents=True)
    canonical_book.rename(wrong_book)

    for series, count in (("Animal Man", 2), ("New X-Men", 3)):
        for volume in range(1, count + 1):
            path = (
                config.comics_root
                / series
                / "Specials"
                / f"{series} - {volume:03d} - {series} Vol. {volume}.cbz"
            )
            _comic(
                path,
                series=series,
                number=str(volume),
                title=f"{series} Vol. {volume}",
                format_="Trade Paperback",
            )

    spider = (
        config.comics_root
        / "Spider-Man - Life Story"
        / "Specials"
        / "Spider-Man - Life Story - 2nd edition.cbz"
    )
    _comic(
        spider,
        series="Spider-Man - Life Story",
        number="",
        title="2nd edition",
        format_="Trade Paperback",
    )

    result = check_library(tmp_path / "Libraries", config)
    plan = plan_library_repairs(result, config)

    assert len(plan.actions) == 6
    assert len(plan.manual) == 1
    assert plan.manual[0].path == spider


def test_library_fix_cli_decline_keeps_library_unchanged(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.comics_root is not None
    legacy = config.comics_root / "Saga" / "Specials" / "Saga - 001 - Saga Vol. 1.cbz"
    _comic(
        legacy,
        series="Saga",
        number="1",
        title="Saga Vol. 1",
        format_="Trade Paperback",
    )
    before = legacy.read_bytes()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[paths]\nbooks = "{config.books_root}"\ncomics = "{config.comics_root}"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["library-fix", str(config.comics_root), "--config", str(config_path)],
        input="n\n",
    )

    assert result.exit_code == 0, result.output
    assert "No changes made" in result.output
    assert legacy.read_bytes() == before
    assert not (config.comics_root / "Saga" / "Specials" / "Saga - v01 - Saga Vol. 1.cbz").exists()
