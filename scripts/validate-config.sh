#!/bin/bash

# Validate Datadog configuration script

# Function to check if the required configurations are set
validate_config() {
    if [ -z "$DD_API_KEY" ]; then
        echo "Error: Datadog API key is not set."
        exit 1
    fi
    if [ -z "$DD_APP_KEY" ]; then
        echo "Error: Datadog Application key is not set."
        exit 1
    fi
    echo "Datadog configuration is valid."
}

# Main script execution
validate_config
