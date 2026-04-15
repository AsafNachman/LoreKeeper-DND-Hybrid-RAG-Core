📜 LoreKeeper: Production-Grade Hybrid RAG Engine
"A production-hardened E2E Hybrid RAG engine built on a modular, service-oriented architecture. Leveraging ensemble search (BM25 + Vector) and a self-healing logic loop, it resolves the 'unstructured data' challenge across any technical domain, using D&D's intricate rulebooks as a high-complexity stress test."

🚀 Overview
LoreKeeper is a high-performance Retrieval-Augmented Generation (RAG) system designed to provide hyper-accurate answers from complex, multi-structured document archives. While currently deployed with Dungeons & Dragons 5e rulebooks, its domain-agnostic decoupled core is engineered to be a universal "brain" for any technical PDF library.

This project was developed over 10 days of rapid iteration, moving from a monolithic script to a professional, modular infrastructure capable of running high-stakes inference locally.

🧠 Core AI Features (The "Applied AI" Edge)
Hybrid Retrieval Pipeline: Merges BM25 (Lexical) and Vector (Semantic) search using ensemble fusion. This ensures that specific technical terms (like "Armor Class") are never missed while maintaining semantic context.

Self-Correcting Logic Loop: A dedicated "Critic" layer analyzes retrieved context against generated answers to eliminate hallucinations before they reach the user.

FlashRank Reranking: Implements a cross-encoder reranking step to prioritize the most relevant document chunks within the LLM's context window.

Fuzzy Query Cleaning: A built-in pre-processing utility that corrects user typos and normalizes technical jargon without the latency of an LLM call.

🏗️ Architecture & Infrastructure (The "Senior" Engineering)
Service-Oriented Design: The system is fully modular, separating the Core Retrieval Engine from the UI/CLI interfaces, Data Storage, and Observability Services.

Hardware-Aware Optimization: Designed for local inference via Ollama, featuring:

Async Non-Blocking Warmup: The UI renders instantly while the GPU prewarms in a background thread.

VRAM Management: Intelligent polling to ensure the model is fully loaded in VRAM before processing queries.

Production-Ready FileSystem: Structured data management with dedicated paths for persistent storage, ingested lore, and automated error logging.

Dockerized Deployment: Ready-to-use Docker configuration for consistent environment orchestration.

🛠️ Tech Stack
Orchestration: Python 3.12, Streamlit

LLM & Embedding: Ollama (Llama 3 / Mistral), OpenAI (optional)

Vector Database: ChromaDB

Retrieval: BM25 (Rank-BM25), FlashRank

Automation: Bash (Setup Scripting)

⚡ Quick Start
1. Prerequisites
Python 3.12+

Ollama (installed and running)

Docker (optional)

2. Automated Installation
We've included a developer-experience (DX) script to set up your environment instantly:

Bash
# Clone the repository
git clone https://github.com/AsafNachman/LoreKeeper-DND-Hybrid-RAG-Core.git
cd LoreKeeper-DND-Hybrid-RAG-Core

# Run the automated setup
bash setup.sh
3. Running the Application
Bash
streamlit run app.py
📈 Why Dungeons & Dragons?
D&D 5e serves as the ultimate stress test for RAG systems due to:

High Data Density: Hundreds of interconnected rules across multiple books.

Specific Jargon: Terms that mean different things in common English vs. Game Mechanics.

Complex Retrieval: Needs to understand the difference between a "Flavor Text" and a "Rule Constraint."

If it can solve D&D, it can solve your company's technical documentation.

🪵 Development Roadmap (v2.1.2)
Check out the Simplified ChangeLog to see how this project evolved from a 1,800-line monolith to a modular professional engine in just 10 days.

📄 License
Distributed under the MIT License. See LICENSE for more information.

Contact: Asaf Nachman - Computer Science Student (97 GPA) & AI Infrastructure Enthusiast.