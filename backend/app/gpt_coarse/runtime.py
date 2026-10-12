"""Versioned GPT dispatch; preserves expert9 for historical contexts."""
from . import predict as expert9
from . import expert8

ENGINES = {'expert9': expert9, 'expert8': expert8}


def method(model):
    return next((key for key, engine in ENGINES.items() if engine.valid(model)), None)


def features(name):
    return ENGINES[name].FEATURES


def valid(model, name):
    return name in ENGINES and ENGINES[name].valid(model)


def explain(record, model, name):
    return ENGINES[name].explain(record, model)
