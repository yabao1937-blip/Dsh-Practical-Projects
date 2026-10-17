"""Small GPT decision input store, opt-in for the laptop's read-only runtime.

This adapter preserves the legacy model representation and raw setting/auto
values.  Caller-specific time gates still live in shared_snapshot.  It does not
load historical coal tables, compute state revisions or modify DS storage.
"""
from sqlalchemy import select

from ..gpt_coarse.repository import active as active_gpt_expert
from ..models import AutoState, CoarseModel, Setting


AUTO_KEYS = frozenset(('coarseModelVariants', 'densityLastMoveAt', 'densityActionLatch'))


def load_decision_store(db, model_at=None):
    """Four small-table reads; model_at=None retains active()'s original clock."""
    store = {'coarseModel': None, 'coarseModelVariants': {}}
    for row in db.scalars(select(Setting)):
        if row.key == 'density_guide':
            store['densityGuide'] = row.value
    for row in db.scalars(select(AutoState)):
        if row.key in AUTO_KEYS:
            store[row.key] = row.value

    rows = list(db.scalars(select(CoarseModel)))
    if rows:
        group = {'production': 'pls'}
        production = None
        for row in rows:
            # Exact existing state.load_store representation: deliberately do
            # not add feature_names or otherwise change model/context hashes.
            model = {
                'type': row.method, 'intercept': row.intercept, 'coefs': row.coefs,
                'means': row.means, 'stds': row.stds, 'stdCoef': row.std_coef,
                'metrics': row.metrics, 'imputeMeans': row.impute_means,
                'n': row.n, 'tolerance': row.tolerance, 'range': row.train_range,
            }
            if row.method == 'pls':
                model['A'] = row.pls_A
            if (row.metrics or {}).get('inputPolicy') == 'clip-training-range':
                model['inputPolicy'] = row.metrics['inputPolicy']
                model['inputBounds'] = row.metrics.get('inputBounds')
                model['config'] = row.metrics.get('config')
            group[row.method] = model
            if row.is_current:
                production = row.method
            group['trainedAt'] = row.trained_at
            group['n'] = row.n
            group['tolerance'] = row.tolerance
            group['range'] = row.train_range
        group['production'] = production or 'pls'
        store['coarseModel'] = group

    store['gptExpertModel'] = active_gpt_expert(db, model_at)
    return store
