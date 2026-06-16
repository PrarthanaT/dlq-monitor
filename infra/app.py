#!/usr/bin/env python3
import aws_cdk as cdk

from stacks.dlq_monitor_stack import DlqMonitorStack

app = cdk.App()
DlqMonitorStack(app, "DlqMonitorStack", description="DLQ Monitor - automated dead letter queue triage and retry")
app.synth()
