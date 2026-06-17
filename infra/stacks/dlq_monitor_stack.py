from pathlib import Path

from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cw,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_sns as sns,
    aws_sqs as sqs,
)
from aws_cdk.aws_apigatewayv2 import (
    CorsHttpMethod,
    CorsPreflightOptions,
    HttpApi,
    HttpMethod,
)
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct

PROJECT_ROOT = str(Path(__file__).resolve().parents[2])

LAMBDA_EXCLUDE = [
    "infra",
    ".git",
    "__pycache__",
    "*.pyc",
    ".env",
    ".env.example",
    "localstack-init",
    "Dockerfile",
    "docker-compose.yml",
    ".claude",
]


class DlqMonitorStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── SQS ──────────────────────────────────────────────────────
        dlq = sqs.Queue(
            self,
            "DLQ",
            queue_name="my-service-dlq",
            retention_period=Duration.days(14),
            visibility_timeout=Duration.seconds(30),
        )

        source_queue = sqs.Queue(
            self,
            "SourceQueue",
            queue_name="my-service-queue",
            visibility_timeout=Duration.seconds(30),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq),
        )

        # ── DynamoDB ─────────────────────────────────────────────────
        retry_table = dynamodb.Table(
            self,
            "RetryTracking",
            table_name="dlq-retry-tracking",
            partition_key=dynamodb.Attribute(
                name="message_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            time_to_live_attribute="ttl",
        )

        # ── SNS ──────────────────────────────────────────────────────
        alerts_topic = sns.Topic(
            self, "AlertsTopic", topic_name="dlq-alerts", display_name="DLQ Monitor Alerts"
        )

        # ── Lambda code bundle ───────────────────────────────────────
        lambda_code = _lambda.Code.from_asset(
            PROJECT_ROOT,
            exclude=LAMBDA_EXCLUDE,
            bundling=BundlingOptions(
                image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                platform="linux/amd64",
                command=[
                    "bash",
                    "-c",
                    "pip install -r requirements.txt -t /asset-output --quiet && "
                    "cp -a app lambda_handlers /asset-output/",
                ],
            ),
        )

        shared_env = {
            "DLQ_URL": dlq.queue_url,
            "SOURCE_QUEUE_URL": source_queue.queue_url,
            "SNS_TOPIC_ARN": alerts_topic.topic_arn,
            "MAX_RETRY_ATTEMPTS": "3",
            "ALERT_THRESHOLD": "5",
        }

        # ── API Lambda ───────────────────────────────────────────────
        api_fn = _lambda.Function(
            self,
            "ApiFunction",
            function_name="dlq-monitor-api",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_handlers.api.handler",
            code=lambda_code,
            timeout=Duration.seconds(30),
            memory_size=256,
            environment=shared_env,
        )

        # ── Poller Lambda ────────────────────────────────────────────
        poller_fn = _lambda.Function(
            self,
            "PollerFunction",
            function_name="dlq-monitor-poller",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_handlers.poller.handler",
            code=lambda_code,
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                **shared_env,
                "DYNAMODB_TABLE_NAME": retry_table.table_name,
            },
        )

        # ── IAM: API Lambda (least privilege) ────────────────────────
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "sqs:ReceiveMessage",
                    "sqs:DeleteMessage",
                    "sqs:SendMessage",
                    "sqs:GetQueueAttributes",
                    "sqs:GetQueueUrl",
                ],
                resources=[dlq.queue_arn],
            )
        )
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sqs:SendMessage"],
                resources=[source_queue.queue_arn],
            )
        )
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sns:Publish"],
                resources=[alerts_topic.topic_arn],
            )
        )

        # ── IAM: Poller Lambda (least privilege) ─────────────────────
        poller_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "sqs:ReceiveMessage",
                    "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes",
                    "sqs:GetQueueUrl",
                ],
                resources=[dlq.queue_arn],
            )
        )
        poller_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sqs:SendMessage"],
                resources=[source_queue.queue_arn],
            )
        )
        poller_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sns:Publish"],
                resources=[alerts_topic.topic_arn],
            )
        )
        poller_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
                resources=[retry_table.table_arn],
            )
        )
        poller_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {"cloudwatch:namespace": "DLQMonitor"}
                },
            )
        )

        # ── API Gateway HTTP API ─────────────────────────────────────
        api_integration = HttpLambdaIntegration("ApiIntegration", handler=api_fn)

        http_api = HttpApi(
            self,
            "HttpApi",
            api_name="dlq-monitor-api",
            cors_preflight=CorsPreflightOptions(
                allow_methods=[
                    CorsHttpMethod.GET,
                    CorsHttpMethod.POST,
                    CorsHttpMethod.DELETE,
                    CorsHttpMethod.OPTIONS,
                ],
                allow_origins=["*"],
                allow_headers=["Content-Type", "Authorization"],
            ),
        )

        http_api.add_routes(
            path="/{proxy+}",
            methods=[HttpMethod.GET, HttpMethod.POST, HttpMethod.DELETE],
            integration=api_integration,
        )
        http_api.add_routes(
            path="/",
            methods=[HttpMethod.GET],
            integration=api_integration,
        )

        # ── EventBridge: poll every 1 minute ─────────────────────────
        poll_rule = events.Rule(
            self,
            "PollerSchedule",
            rule_name="dlq-monitor-poller-schedule",
            schedule=events.Schedule.rate(Duration.minutes(1)),
        )
        poll_rule.add_target(targets.LambdaFunction(poller_fn))

        # ── CloudWatch Dashboard ─────────────────────────────────────
        dashboard = cw.Dashboard(
            self,
            "MonitorDashboard",
            dashboard_name="DLQMonitor",
            period_override=cw.PeriodOverride.AUTO,
        )

        metric_kwargs = {
            "namespace": "DLQMonitor",
            "period": Duration.minutes(5),
            "statistic": "Sum",
        }

        messages_retried = cw.Metric(metric_name="messages_retried", **metric_kwargs)
        messages_dead = cw.Metric(metric_name="messages_dead", **metric_kwargs)
        alerts_sent = cw.Metric(metric_name="alerts_sent", **metric_kwargs)

        dashboard.add_widgets(
            cw.GraphWidget(
                title="Messages Retried",
                left=[messages_retried],
                width=8,
                height=6,
            ),
            cw.GraphWidget(
                title="Messages Dead-Lettered",
                left=[messages_dead],
                width=8,
                height=6,
            ),
            cw.GraphWidget(
                title="Alerts Sent",
                left=[alerts_sent],
                width=8,
                height=6,
            ),
        )

        # ── Outputs ──────────────────────────────────────────────────
        CfnOutput(self, "ApiUrl", value=http_api.url, description="API Gateway endpoint URL")
        CfnOutput(self, "DlqUrl", value=dlq.queue_url, description="DLQ queue URL")
        CfnOutput(self, "SourceQueueUrl", value=source_queue.queue_url, description="Source queue URL")
        CfnOutput(self, "SnsTopicArn", value=alerts_topic.topic_arn, description="SNS alerts topic ARN")
        CfnOutput(self, "RetryTableName", value=retry_table.table_name, description="DynamoDB retry tracking table")
        CfnOutput(self, "DashboardUrl",
            value=f"https://{self.region}.console.aws.amazon.com/cloudwatch/home#dashboards:name=DLQMonitor",
            description="CloudWatch dashboard URL",
        )
