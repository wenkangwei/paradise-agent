"""Proactive Agent Module — scheduled autonomous messaging.

ProactiveScheduler periodically evaluates whether the agent should proactively
send the user a message, based on:
  - Time since last interaction
  - Recent conversation context
  - Configured interval / cooldown

Messages are queued in-memory per conversation and delivered to Android via
the long-polling endpoint GET /api/agent/proactive/poll.
"""
