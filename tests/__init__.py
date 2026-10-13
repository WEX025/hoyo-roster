"""Explicit composition for synthetic stores."""

import importlib


def candidate_policy_for(game):
    return importlib.import_module(f"hoyo_roster.games.{game}.candidate").candidate_policy()
