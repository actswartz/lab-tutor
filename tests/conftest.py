import dataclasses
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lab_tutor.broker import FIXTURE_DIR
from lab_tutor.config import Settings
from lab_tutor.web import TutorTarget, make_router

SID = "bf1c18e27d60ee7d246f35f5ab4a7815"


@pytest.fixture
def sample():
    return json.loads((FIXTURE_DIR / "sample-lab03.json").read_text())


@pytest.fixture
def settings(tmp_path):
    return Settings(mock=True, spend_file=str(tmp_path / "spend.json"))


def make_app(settings, **kw):
    async def resolve(request, booking_id):
        if booking_id != "b1" and booking_id != "b2":
            return None
        return TutorTarget(SID, "http://broker.test", "s3cret", "/term")

    app = FastAPI()
    router = make_router(settings, resolve, **kw)
    app.include_router(router)
    return app, router


@pytest.fixture
def client(settings):
    app, router = make_app(settings)
    c = TestClient(app)
    c.router_obj = router
    return c
