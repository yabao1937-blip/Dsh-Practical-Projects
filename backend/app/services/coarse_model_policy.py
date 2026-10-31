"""Keep the existing GPT model; retired DS snapshots are not runnable models."""


def is_gpt_group(model):
    if not isinstance(model, dict) or model.get('engine') == 'ds':
        return False
    for method in ('mlr', 'pls'):
        node = model.get(method)
        if not isinstance(node, dict):
            return False
        metrics = node.get('metrics')
        if not isinstance(metrics, dict) or metrics.get('version') != 2:
            return False
    return True


def gpt_only_store(store, *, importing=False):
    """Filter model fields without touching records or changing GPT parameters.

    An obsolete browser payload does not own the current GPT fields: omit those
    keys on import instead of turning a retired DS model into a destructive clear.
    Historical database rows remain available for audit; no database writes here.
    """
    result = dict(store)
    bank = store.get('coarseModelVariants')
    saved = bank.get('gpt') if isinstance(bank, dict) else None
    saved = saved if is_gpt_group(saved) else None
    if 'coarseModelVariants' in result:
        if saved is not None:
            result['coarseModelVariants'] = {'gpt': saved}
        elif importing:
            result.pop('coarseModelVariants')
        else:
            result['coarseModelVariants'] = {}
    if 'coarseModel' in result and not is_gpt_group(result['coarseModel']):
        if saved is not None:
            result['coarseModel'] = saved
        elif importing:
            result.pop('coarseModel')
        else:
            result['coarseModel'] = None
    return result
