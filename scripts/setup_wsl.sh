#!/bin/bash
# Setup script for Spot Arbitrage Cluster in WSL
set -e

echo "=========================================="
echo "Spot Arbitrage Cluster - WSL Setup"
echo "=========================================="
echo ""

# Check if running in WSL
if [ -z "$WSL_DISTRO_NAME" ] && [ -z "$WSLENV" ]; then
    echo "⚠️  Warning: This doesn't appear to be a WSL environment"
    echo "   The script will continue, but CRIU may not work properly"
    echo ""
fi

# Check WSL version (if possible)
if command -v wsl.exe &> /dev/null; then
    echo "Checking WSL version..."
    wsl.exe --version 2>/dev/null || echo "Could not determine WSL version"
    echo ""
fi

# Update package list
echo "📦 Updating package list..."
sudo apt update

# Install CRIU
echo "📦 Installing CRIU (Checkpoint/Restore In Userspace)..."
sudo apt install -y criu

# Verify CRIU installation
echo "✅ Verifying CRIU installation..."
if criu --version > /dev/null 2>&1; then
    echo "   CRIU version: $(criu --version | head -n1)"
else
    echo "   ⚠️  CRIU installation verification failed"
fi

# Install Python and pip
echo "📦 Installing Python 3 and pip..."
sudo apt install -y python3 python3-pip python3-venv

# Install AWS CLI (optional, user may have it already)
if ! command -v aws &> /dev/null; then
    echo "📦 Installing AWS CLI..."
    sudo apt install -y awscli
else
    echo "✅ AWS CLI already installed"
fi

# Create virtual environment
echo "🐍 Creating Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "✅ Virtual environment created"
else
    echo "✅ Virtual environment already exists"
fi

# Activate virtual environment
echo "🔌 Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "📦 Upgrading pip..."
pip install --upgrade pip

# Install Python dependencies
echo "📦 Installing Python dependencies..."
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
    echo "✅ Dependencies installed"
else
    echo "⚠️  requirements.txt not found, skipping dependency installation"
fi

# Make scripts executable
echo "🔧 Setting script permissions..."
if [ -f "checkpoint/criu_wrapper.sh" ]; then
    chmod +x checkpoint/criu_wrapper.sh
    echo "✅ Made criu_wrapper.sh executable"
fi

# Create necessary directories
echo "📁 Creating necessary directories..."
mkdir -p /tmp/criu_dump
mkdir -p storage
echo "✅ Directories created"

# Verify setup
echo ""
echo "=========================================="
echo "Setup Verification"
echo "=========================================="

# Check Python
if command -v python3 &> /dev/null; then
    echo "✅ Python: $(python3 --version)"
else
    echo "❌ Python not found"
fi

# Check pip
if command -v pip &> /dev/null; then
    echo "✅ pip: $(pip --version | cut -d' ' -f1-2)"
else
    echo "❌ pip not found"
fi

# Check CRIU
if command -v criu &> /dev/null; then
    echo "✅ CRIU: $(criu --version | head -n1)"
else
    echo "❌ CRIU not found"
fi

# Check AWS CLI
if command -v aws &> /dev/null; then
    echo "✅ AWS CLI: $(aws --version)"
else
    echo "⚠️  AWS CLI not found (optional)"
fi

# Check virtual environment
if [ -d "venv" ]; then
    echo "✅ Virtual environment: venv/"
else
    echo "❌ Virtual environment not found"
fi

# Check Python packages
if [ -d "venv" ]; then
    source venv/bin/activate
    echo ""
    echo "Installed Python packages:"
    pip list | grep -E "(boto3|pyyaml)" || echo "   (packages may need installation)"
fi

echo ""
echo "=========================================="
echo "Setup Complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Activate virtual environment:"
echo "   source venv/bin/activate"
echo ""
echo "2. Configure AWS credentials:"
echo "   aws configure"
echo "   # Or set environment variables:"
echo "   export AWS_ACCESS_KEY_ID='your-key'"
echo "   export AWS_SECRET_ACCESS_KEY='your-secret'"
echo ""
echo "3. Test CRIU (optional):"
echo "   sudo criu check"
echo ""
echo "4. Run the project:"
echo "   python worker/job_runner.py"
echo "   # Or:"
echo "   make run-worker"
echo ""
echo "For troubleshooting, see WSL_SETUP.md"
echo ""

