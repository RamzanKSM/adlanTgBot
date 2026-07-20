from app.legal.documents import DOCS_ROOT, LEGAL_DOCUMENTS, load_legal_document_page, render_legal_document_page


def test_legal_document_metadata_contains_required_documents() -> None:
    assert [document.key for document in LEGAL_DOCUMENTS] == [
        "offer",
        "privacy",
        "refunds",
        "community_rules",
    ]


def test_load_legal_document_page_reads_numbered_markdown_in_order(tmp_path) -> None:
    docs_root = tmp_path / "docs"
    offer_dir = docs_root / "offer"
    offer_dir.mkdir(parents=True)
    (offer_dir / "offer_02.md").write_text("Second page", encoding="utf-8")
    (offer_dir / "offer_01.md").write_text("First page", encoding="utf-8")

    page = load_legal_document_page("offer", 1, docs_root=docs_root)

    assert page is not None
    assert page.meta.title == "Оферта"
    assert page.page_number == 1
    assert page.total_pages == 2
    assert page.text == "First page"
    assert render_legal_document_page(page) == "📄 Оферта\n📃 Страница 1 из 2\n\nFirst page"


def test_load_legal_document_page_clamps_to_existing_page(tmp_path) -> None:
    docs_root = tmp_path / "docs"
    privacy_dir = docs_root / "privacy"
    privacy_dir.mkdir(parents=True)
    (privacy_dir / "privacy_01.md").write_text("Only page", encoding="utf-8")

    page = load_legal_document_page("privacy", 10, docs_root=docs_root)

    assert page is not None
    assert page.page_number == 1
    assert page.total_pages == 1
    assert page.text == "Only page"


def test_load_legal_document_page_returns_placeholder_for_missing_files(tmp_path) -> None:
    page = load_legal_document_page("refunds", 1, docs_root=tmp_path / "docs")

    assert page is not None
    assert page.page_number == 1
    assert page.total_pages == 1
    assert page.text == "📄 Документ скоро будет опубликован."


def test_load_legal_document_page_rejects_unknown_document(tmp_path) -> None:
    assert load_legal_document_page("unknown", 1, docs_root=tmp_path / "docs") is None


def test_packaged_legal_document_pages_are_telegram_sized() -> None:
    for document in LEGAL_DOCUMENTS:
        page_files = sorted((DOCS_ROOT / document.key).glob(f"{document.key}_[0-9][0-9].md"))

        assert page_files, f"{document.key} has no pages"
        for page_file in page_files:
            text = page_file.read_text(encoding="utf-8").strip()
            assert page_file.stem.startswith(f"{document.key}_")
            page = load_legal_document_page(document.key, int(page_file.stem.rsplit("_", 1)[1]))

            assert page is not None
            rendered = render_legal_document_page(page)

            assert len(text) >= 1000, f"{page_file} is too short"
            assert len(rendered) < 3900, f"{page_file} is too long for a Telegram message"
