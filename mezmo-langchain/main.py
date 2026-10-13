import sys

if any(arg in ("-h", "--help", "help") for arg in sys.argv[1:]):
    print("usage: OPENAI_API_KEY=... python3 main.py\nSend one hello-world prompt through LangChain to OpenAI.")
    sys.exit(0)

from langchain_openai import ChatOpenAI

llm = ChatOpenAI()
print(llm.invoke("Hello, world!"))

