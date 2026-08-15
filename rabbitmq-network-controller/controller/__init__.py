"""RabbitMQ Network Controller.

A standalone Linux daemon that dynamically shapes the *real* network bandwidth
available to RabbitMQ traffic (AMQP 5672 / management 15672) using the kernel
traffic-control subsystem (``tc``) plus optional netfilter packet marking.

The package never touches RabbitMQ itself: no broker configuration, no queue
names, no credentials.  All shaping happens at the host networking layer and is
fully reverted on shutdown.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
