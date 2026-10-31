"""DS retirement must not remove or alter the retained GPT model."""
import copy
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.models import Base, CoarseModel
from app.services import migrate
from app.services.coarse_model_policy import gpt_only_store, is_gpt_group
from app.services.modeling import predict_coarse_ash
from app.services.state import load_store


def gpt():
    return json.loads((Path(__file__).parents[1] / 'data/coarse_v2_js.json').read_text('utf-8'))['coarseModel']


def test_retired_routes_and_engine_are_rejected():
    client = TestClient(app)
    assert client.get('/api/v1/training/coarse-forward').status_code == 404
    response = client.post('/api/v1/training/coarse-model?engine=ds')
    assert response.status_code == 422
    params = client.get('/openapi.json').json()['paths']['/api/v1/training/coarse-model']['post']['parameters']
    assert next(p for p in params if p['name'] == 'engine')['schema']['default'] == 'gpt'


def test_old_snapshots_cannot_be_labeled_gpt_or_mutate_input():
    retired = {'engine': 'gpt', 'mlr': {'metrics': {}}, 'pls': {'metrics': {}}}
    assert not is_gpt_group(retired)
    retained = gpt()
    state = {'coarseModel': retired, 'coarseModelVariants': {'ds': retired, 'gpt': retained},
             'coarseCoal': [{'ash_content': 12.3}]}
    before = copy.deepcopy(state)
    cleaned = gpt_only_store(state)
    assert cleaned['coarseModel'] == retained
    assert cleaned['coarseModelVariants'] == {'gpt': retained}
    assert state == before
    assert cleaned['coarseCoal'] == state['coarseCoal']
    assert gpt_only_store({'coarseModel': retired})['coarseModel'] is None
    assert predict_coarse_ash({'ash_content': 12.3}, {}) is None
    assert predict_coarse_ash({'level': 20}, {'intercept': 12.3, 'coefs': []}) == 12.3


def test_obsolete_browser_does_not_clear_existing_gpt(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + (tmp_path / 'isolated.db').as_posix())
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(migrate, 'SessionLocal', factory)
    retained = gpt()
    assert migrate.replace({'coarseModel': retained, 'coarseModelVariants': {'gpt': retained}})['ok']
    with factory() as db:
        before = load_store(db)['coarseModel']
    retired = {'engine': 'ds', 'production': 'mlr', 'mlr': {'intercept': 99, 'coefs': []}}
    result = migrate.replace({'coarseModel': retired, 'coarseModelVariants': {'ds': retired}})
    assert result['ok']
    assert 'coarse_models' in result['untouched']
    with factory() as db:
        after = load_store(db)
        assert db.query(CoarseModel).count() == 2
        assert after['coarseModel'] == before
        assert after['coarseModelVariants'] == {'gpt': retained}
    engine.dispose()
