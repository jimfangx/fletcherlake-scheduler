"""Leave dispatcher capacity for receipt replay while delivering bounded outbox batches."""

MAX_ACTIVE_COMMANDS = 64
COMMAND_DELIVERY_BATCH = 32
