#!/bin/bash
set -e

echo "=== zae-limiter LocalStack Initialization ==="

# Install zae-limiter first
echo "Installing zae-limiter..."
SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 pip install -q /zae-limiter

# Wait for LocalStack to be ready
echo "Waiting for LocalStack..."
until aws --endpoint-url=$AWS_ENDPOINT_URL dynamodb list-tables 2>/dev/null; do
  echo "  LocalStack not ready, waiting..."
  sleep 2
done
echo "LocalStack is ready!"

# Deploy infrastructure using zae-limiter CLI
# This creates DynamoDB table, Lambda function, and event source mapping via CloudFormation
echo "Deploying zae-limiter infrastructure..."
zae-limiter deploy \
  --name $NAME \
  --region ${AWS_DEFAULT_REGION:-us-east-1} \
  --endpoint-url $AWS_ENDPOINT_URL \
  --wait

echo "Infrastructure deployed!"

# Seed demo data
echo "Seeding demo data..."
python /opt/seed_data.py

echo "=== Initialization Complete ==="
