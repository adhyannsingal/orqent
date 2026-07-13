"""Infrastructure layer — concrete adapters implementing domain ports.

All framework/vendor coupling lives here: SQLAlchemy repositories, the ChromaDB
adapter, the task queue + worker runtime, security primitives, and (in the
execution phase) the LangChain-backed ``AgentRunner``. Nothing outside this
package imports LangChain or a database driver.
"""
