#!/bin/bash
set -e

echo "Initializing LocalStack resources..."

awslocal sqs create-queue --queue-name my-service-queue

awslocal sqs create-queue --queue-name my-service-dlq

awslocal sns create-topic --name dlq-alerts

DLQ_ARN=$(awslocal sqs get-queue-attributes \
  --queue-url http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/my-service-dlq \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' --output text)

SOURCE_ARN=$(awslocal sqs get-queue-attributes \
  --queue-url http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/my-service-queue \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' --output text)

awslocal sqs set-queue-attributes \
  --queue-url http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/my-service-queue \
  --attributes "{\"RedrivePolicy\": \"{\\\"deadLetterTargetArn\\\":\\\"${DLQ_ARN}\\\",\\\"maxReceiveCount\\\":\\\"3\\\"}\"}"

TOPIC_ARN=$(awslocal sns list-topics --query 'Topics[0].TopicArn' --output text)
awslocal sns subscribe \
  --topic-arn "$TOPIC_ARN" \
  --protocol sqs \
  --notification-endpoint "$DLQ_ARN"

echo "LocalStack resources created successfully."
echo "  Source queue: my-service-queue"
echo "  DLQ:          my-service-dlq"
echo "  SNS topic:    dlq-alerts"
