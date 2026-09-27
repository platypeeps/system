from openai import OpenAI
from openinference.instrumentation.openai import OpenAIInstrumentor
from phoenix.otel import register

tracer_provider = register()
OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)

client = OpenAI()

conversation = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is the capital of France?"},
]

response = client.chat.completions.create(
    model="gpt-3.5-turbo",
    messages=conversation,
)

assistant_reply = response.choices[0].message.content
print("Assistant:", assistant_reply)
