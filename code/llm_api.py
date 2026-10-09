import logging
from concurrent.futures import ThreadPoolExecutor
from langchain_groq import ChatGroq
from config import GROQ_API_KEY, GROQ_MODEL_NAME, agent_type
from system_prompt import get_prompt
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from conversation_processor.conversation_processor import process_conversation
from history import get_session_history

log = logging.getLogger(__name__)


def get_groq_response(input_text, session_id, request_type, domain):
    
    # Choose prompt based on request type & domain
    prompt_type = "system"
    system_prompt = get_prompt(domain, request_type, prompt_type)

    
    # Rest of your existing code...
    prompt = ChatPromptTemplate.from_messages([
        ("system", "{system}"),
        MessagesPlaceholder(variable_name="history"),
        ("human", "{input}")
    ])


    """
    Generate a Groq LLM response using RunnableWithMessageHistory for chat memory.
    
    Args:
        input_text: User input text
        session_id: Session identifier
        process_async: Whether to process conversation asynchronously (default: True)
    """


    # Create the LLM
    llm = ChatGroq(groq_api_key=GROQ_API_KEY, model=GROQ_MODEL_NAME)
    # Create the chain
    chain = prompt | llm
    
    # Wrap the chain with message history
    chain_with_history = RunnableWithMessageHistory(
        chain,
        get_session_history,
        input_messages_key="input",
        history_messages_key="history",
    )
    
    # Configure the session
    config = {"configurable": {"session_id": session_id}}
    
    # Get response with history
    response = chain_with_history.invoke(
        {
            "input": input_text,
            "system": system_prompt
        },
        config=config
    )


    bot_response = response.content


    # Handle conversation processing
    # Process conversation asynchronously to avoid blocking the response
    _process_conversation_async(input_text, session_id, request_type, domain)



    return bot_response



def _process_conversation_async(input_text, session_id, request_type, domain):
    """Process conversation asynchronously using ThreadPoolExecutor."""
    def process_in_background():
        try:
            process_conversation(input_text, session_id, request_type, domain)
            log.info("Async conversation processing completed")
        except Exception as processing_error:
            # The chat reply already went out, so this is not fatal. Keep the
            # traceback: it is the only record of a failure in this thread.
            log.warning(
                "Async conversation processing failed: %s",
                processing_error,
                exc_info=True,
            )
    
    # Submit to thread pool for background processing
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(process_in_background)
