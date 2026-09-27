from langchain_openai import ChatOpenAI

llm = ChatOpenAI()
print(llm.invoke("Hello, world!"))

