#!/bin/bash

# Setup script to initialize and setup the Datadog observability pipeline
echo "Initializing Datadog observability pipeline..."

# Set your Datadog API key and application key here
export DD_API_KEY='your_api_key'
export DD_APP_KEY='your_app_key'

# Install the Datadog agent
DD_AGENT_MAJOR_VERSION=7
DD_AGENT_VERSION=$(curl -s https://api.datadoghq.com/api/v1/agent/versions | jq -r '.[] | .version' | sort -V | tail -n 1)
wget -qO - https://apt.datadoghq.com/gpg.key | apt-key add -
echo "deb https://apt.datadoghq.com/ $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/datadog.list
sudo apt-get update
sudo apt-get install -y datadog-agent=$DD_AGENT_VERSION

# Start the Datadog agent
sudo systemctl start datadog-agent

# Enable the Datadog agent to start on boot
sudo systemctl enable datadog-agent
echo "Datadog observability pipeline setup complete!"