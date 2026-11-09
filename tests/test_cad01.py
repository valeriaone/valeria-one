"""cad01 (v0.57.0): design, architecture and engineering businesses get the Studio (projects, rooms, CAD / SketchUp / Photoshop
files, client portal, visualizer) — the trade is named for all three, and their live demo shows sample projects with rooms."""
from sqlalchemy import func, select

from valeria_one import db, packs


def test_the_trade_covers_design_architecture_and_engineering():
    ind = packs.industry("design")
    assert ind.studio is True
    assert "ingeniería" in ind.name["es"] and "arquitectura" in ind.name["es"] and "engineering" in ind.name["en"]


def test_a_design_demo_shows_sample_projects_with_rooms(monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    from valeria_one.sandbox import seed
    seed.seed()
    with db.tx() as conn:
        rid = conn.execute(select(db.businesses.c.id).where(db.businesses.c.name == "Rodrigo Sánchez Interiorismo")).scalar()
        projects = conn.execute(select(db.projects.c.id, db.projects.c.name, db.projects.c.budget_minor, db.projects.c.contact_id)
                                .where(db.projects.c.business_id == rid)).all()
        rooms = conn.execute(select(func.count()).select_from(db.project_rooms)
                             .where((db.project_rooms).c.business_id == rid)).scalar()
        other = conn.execute(select(func.count()).select_from(db.projects).join(db.businesses, db.businesses.c.id == db.projects.c.business_id)
                             .where(db.businesses.c.industry != "design")).scalar()
    assert len(projects) == 2 and all(p.contact_id for p in projects) and {p.name for p in projects} == {"Cocina y sala, 60 m²", "Villa completa"}
    assert rooms == 5 and other == 0                                                        # only design demos get them
