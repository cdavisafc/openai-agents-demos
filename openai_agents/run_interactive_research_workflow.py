import argparse
import asyncio
import sys
from typing import Dict, List

from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter

from openai_agents.workflows.interactive_research_workflow import (
    InteractiveResearchWorkflow,
)
from openai_agents.workflows.research_agents.research_models import (
    ClarificationInput,
    ResearchInteraction,
    SingleClarificationInput,
    UserQueryInput,
)


async def run_interactive_research_with_clarifications(
    client: Client, query: str, workflow_id: str
):
    """Run interactive research with clarifying questions"""
    print(f"🤖 Starting interactive research: {query}")

    # Check if workflow exists and is running
    handle = None
    start_new = True

    try:
        handle = client.get_workflow_handle(workflow_id)
        print("Checking if workflow is already running...")

        try:
            status = await handle.query(InteractiveResearchWorkflow.get_status)
            if status and status.status not in ["completed", "failed", "timed_out", "terminated", "canceled"]:
                print("Found existing running workflow, using it...")
                start_new = False
            else:
                print("Existing workflow is not running, will start a new one...")
        except Exception:
            print("Could not query existing workflow, will start a new one...")

    except Exception:
        print("Workflow not found, will start a new one...")

    if start_new:
        import time
        unique_id = f"{workflow_id}-{int(time.time())}"
        print(f"Starting new research workflow: {unique_id}")

        try:
            handle = await client.start_workflow(
                InteractiveResearchWorkflow.run,
                args=[None, False],
                id=unique_id,
                task_queue="openai-agents-task-queue",
            )
        except Exception as start_error:
            print(f"❌ Failed to start workflow: {start_error}")
            raise

    if not handle:
        raise RuntimeError("Failed to get workflow handle")

    # Start the research process if it's a new workflow or not yet started
    current_status = await handle.query(InteractiveResearchWorkflow.get_status)
    if not current_status or current_status.status == "pending":
        print(f"🔄 Initiating research for: {query}")
        await handle.execute_update(
            InteractiveResearchWorkflow.start_research, UserQueryInput(query=query)
        )

    # Interactive loop for Q&A
    while True:
        try:
            status = await handle.query(InteractiveResearchWorkflow.get_status)

            if not status:
                await asyncio.sleep(1)
                continue

            # States for asking questions
            if status.status in ["awaiting_clarifications", "collecting_answers"]:
                print(
                    f"\n❓ I need to ask you some clarifying questions to provide better research."
                )
                print("-" * 60)

                while status.get_current_question() is not None:
                    current_question = status.get_current_question()
                    print(
                        f"Question {status.current_question_index + 1} of {len(status.clarification_questions or [])}"
                    )
                    print(f"{current_question}")

                    answer = input("Your answer: ").strip()

                    if answer.lower() in ["exit", "quit", "end", "done"]:
                        print("Ending research session...")
                        await handle.signal(InteractiveResearchWorkflow.end_workflow_signal)
                        return # Exit the function entirely

                    status = await handle.execute_update(
                        InteractiveResearchWorkflow.provide_single_clarification,
                        SingleClarificationInput(
                            question_index=status.current_question_index,
                            answer=answer or "No specific preference",
                        ),
                    )
                # After loop, all questions are answered, continue to outer loop to check new status

            # Research has started, time to break the polling loop and wait
            elif status.status == "researching":
                print("\n🔍 Research in progress...")
                print("   📋 Planning searches")
                print("   🌐 Gathering information from sources")
                print("   ✍️  Compiling report")
                print("   ⏳ Please wait...")
                # Break the interactive loop to wait for the final result
                break

            # Workflow is already done, break to get the result
            elif status.status == "completed":
                break

            elif status.status == "pending":
                print("⏳ Starting research...")
                await asyncio.sleep(2)

            else:
                print(f"📊 Unexpected Status: {status.status}, waiting...")
                await asyncio.sleep(2)

        except Exception as e:
            print(f"❌ Error during interaction: {e}")
            # If the workflow fails or is cancelled during interaction, we should exit
            desc = await handle.describe()
            if desc.status not in ("RUNNING", "CONTINUED_AS_NEW"):
                 print(f"Workflow has terminated with status: {desc.status}")
                 return
            await asyncio.sleep(2)

    # After breaking the loop, we wait for the final result.
    # This call will block until the workflow is complete.
    result = await handle.result()

    # Now that the wait is over, print the completion message and result.
    print(f"\n🎉 Research completed!")
    print(f"\n📄 Research Result:")
    print("=" * 60)
    print(result)
    return result


# Keep the old function for backward compatibility
async def run_interactive_research(client: Client, query: str, workflow_id: str):
    """Legacy interactive research - redirects to new pattern"""
    return await run_interactive_research_with_clarifications(client, query, workflow_id)


async def get_workflow_status(client: Client, workflow_id: str):
    """Get the status of an existing workflow"""
    try:
        handle = client.get_workflow_handle(workflow_id)
        status = await handle.query(InteractiveResearchWorkflow.get_status)

        if status:
            print(f"📊 Workflow {workflow_id} status: {status.status}")
            if status.clarification_questions:
                print(f"❓ Pending questions: {len(status.clarification_questions)}")
            if status.final_result:
                print(f"✅ Has final result")
        else:
            print(f"❌ No status available for workflow {workflow_id}")

    except Exception as e:
        print(f"❌ Error getting workflow status: {e}")


async def send_clarifications(
    client: Client, workflow_id: str, responses: Dict[str, str]
):
    """Send clarification responses to an existing workflow"""
    try:
        handle = client.get_workflow_handle(workflow_id)
        result = await handle.execute_update(
            InteractiveResearchWorkflow.provide_clarifications,
            ClarificationInput(responses=responses),
        )
        print(f"✅ Clarifications sent to workflow {workflow_id}")
        print(f"📊 Updated status: {result.status}")

    except Exception as e:
        print(f"❌ Error sending clarifications: {e}")


def parse_clarifications(clarification_args: List[str]) -> Dict[str, str]:
    """Parse clarification responses from command line arguments"""
    responses = {}
    for arg in clarification_args:
        if "=" in arg:
            key, value = arg.split("=", 1)
            responses[key] = value
    return responses


async def main():
    parser = argparse.ArgumentParser(
        description="OpenAI Interactive Research Workflow CLI"
    )
    parser.add_argument("query", nargs="?", help="Research query")
    parser.add_argument(
        "--workflow-id",
        default="interactive-research-workflow",
        help="Workflow ID (default: interactive-research-workflow)",
    )
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="Force start a new workflow session (with unique ID)",
    )
    parser.add_argument(
        "--status", action="store_true", help="Get status of existing workflow"
    )
    parser.add_argument(
        "--clarify",
        nargs="+",
        metavar="KEY=VALUE",
        help="Send clarification responses (e.g., --clarify question_0='travel budget' question_1='March')",
    )

    args = parser.parse_args()

    # Create client
    try:
        client = await Client.connect(
            "localhost:7233",
            data_converter=pydantic_data_converter,
        )
        print(f"🔗 Connected to Temporal server")
    except Exception as e:
        print(f"❌ Failed to connect to Temporal server: {e}")
        print(f"   Make sure Temporal server is running on localhost:7233")
        return

    # Handle different modes
    if args.status:
        await get_workflow_status(client, args.workflow_id)

    elif args.clarify:
        responses = parse_clarifications(args.clarify)
        await send_clarifications(client, args.workflow_id, responses)

    elif args.query:
        # Handle new session flag
        workflow_id = args.workflow_id
        if args.new_session:
            import time

            workflow_id = f"{args.workflow_id}-{int(time.time())}"
            print(f"🆕 Using new session ID: {workflow_id}")

        await run_interactive_research(client, args.query, workflow_id)

    else:
        # Interactive query input
        print("🔍 OpenAI Interactive Research Workflow")
        print("=" * 40)
        query = input("Enter your research query: ").strip()

        if not query:
            print("❌ Query cannot be empty")
            return

        await run_interactive_research(client, query, args.workflow_id)


if __name__ == "__main__":
    asyncio.run(main())