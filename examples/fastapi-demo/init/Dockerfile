FROM python:3.12-slim

WORKDIR /opt

# Install AWS CLI and dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    && curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" \
    && unzip awscliv2.zip \
    && ./aws/install \
    && rm -rf awscliv2.zip aws \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy init scripts
COPY init-aws.sh /opt/init-aws.sh
COPY seed_data.py /opt/seed_data.py

RUN chmod +x /opt/init-aws.sh

# Run initialization
CMD ["/opt/init-aws.sh"]
