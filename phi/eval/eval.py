from uuid import uuid4
from pathlib import Path
from typing import Optional, Union, Callable, List

from pydantic import BaseModel, ConfigDict, field_validator, Field

from phi.assistant import Assistant
from phi.utils.log import logger, set_log_level_to_debug
from phi.utils.timer import Timer


class AccuracyResult(BaseModel):
    score: int = Field(..., description="Accuracy Score between 1 and 10 assigned to the AI Assistant's answer.")
    reason: str = Field(..., description="Detailed reasoning for the accuracy score.")


class EvalResult(BaseModel):
    accuracy_score: int = Field(..., description="Accuracy Score between 1 to 10.")
    accuracy_reason: str = Field(..., description="Reasoning for the accuracy score.")


class Eval(BaseModel):
    # Name of the evaluation
    name: Optional[str] = None
    # UUID of the evaluation (autogenerated if not set)
    eval_id: Optional[str] = Field(None, validate_default=True)
    # Assistant to evaluate
    assistant: Optional[Assistant] = None

    # Question to evaluate
    question: str
    answer: Optional[str] = None
    # Ideal Answer for the question
    ideal_answer: str
    # Result of the evaluation
    result: Optional[EvalResult] = None

    accuracy_evaluator: Optional[Assistant] = None
    accuracy_guidelines: Optional[List[str]] = None
    accuracy_result: Optional[AccuracyResult] = None

    # Save the result to a file
    save_result_to_file: Optional[str] = None

    # debug_mode=True enables debug logs
    debug_mode: bool = False

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("debug_mode", mode="before")
    def set_log_level(cls, v: bool) -> bool:
        if v:
            set_log_level_to_debug()
            logger.debug("Debug logs enabled")
        return v

    @field_validator("eval_id", mode="before")
    def set_eval_id(cls, v: Optional[str]) -> str:
        return v if v is not None else str(uuid4())

    def get_accuracy_evaluator(self) -> Assistant:
        if self.accuracy_evaluator is not None:
            return self.accuracy_evaluator

        try:
            from phi.llm.openai import OpenAIChat
        except ImportError:
            raise ImportError("`openai` is required for the default evaluator.")

        accuracy_guidelines = ""
        if self.accuracy_guidelines is not None and len(self.accuracy_guidelines) > 0:
            accuracy_guidelines = "\nThe AI Assistant's answer must follow these guidelines:\n"
            accuracy_guidelines += "\n- ".join(self.accuracy_guidelines)

        self.accuracy_evaluator = Assistant(
            llm=OpenAIChat(model="gpt-4o-mini"),
            description=f"""\
You are an evaluator tasked with comparing an AI Assistant's answer to an ideal answer for a given question.
You will assess the similarity and accuracy of the Assistant's answer and provide a score on a scale of 1 to 10, where 10 means the answers match exactly.

Here is the question:
<question>
{self.question}
</question>

Here is the ideal answer:
<ideal_answer>
{self.ideal_answer}
</ideal_answer>

Compare the Assistant's answer to the ideal answer. Consider the following aspects:
- Accuracy of information
- Completeness of the answer
- Relevance to the question
- Use of similar key concepts or ideas
- Overall structure and presentation
{accuracy_guidelines}

Provide your reasoning for the comparison, highlighting similarities and differences between the two answers.
Make sure to follow the guidelines and be as objective as possible in your evaluation. Mention the guidelines you followed in your reasoning.
Be specific about what the Assistant's answer includes or lacks compared to the ideal answer.

Based on your comparison, assign a score from 1 to 10, where:
1 = The answers are completely different or the Assistant's answer is entirely incorrect
5 = The Assistant's answer captures some key points but misses others or contains some inaccuracies
10 = The Assistant's answer matches the ideal answer exactly in content and presentation

Only use whole numbers for the score (no decimals).
""",
            output_model=AccuracyResult,
        )
        return self.accuracy_evaluator

    def run(self, answer: Optional[Union[str, Callable]] = None) -> Optional[EvalResult]:
        logger.debug(f"*********** Evaluation Start: {self.eval_id} ***********")

        answer_to_evaluate = None
        if answer is None:
            if self.assistant is not None:
                logger.debug("Getting answer from assistant")
                answer_to_evaluate: str = self.assistant.run(self.question, stream=False)  # type: ignore
            if self.answer is not None:
                answer_to_evaluate = self.answer
        else:
            try:
                if callable(answer):
                    logger.debug("Getting answer from callable")
                    answer_to_evaluate = answer()
                else:
                    answer_to_evaluate = answer
            except Exception as e:
                logger.error(f"Failed to get answer: {e}")
                raise

        if answer_to_evaluate is None:
            raise ValueError("No Answer to evaluate.")
        else:
            self.answer = answer_to_evaluate

        logger.debug("************************ Evaluating ************************")
        logger.debug(f"Question: {self.question}")
        logger.debug(f"Ideal Answer: {self.ideal_answer}")
        logger.debug(f"Answer: {answer_to_evaluate}")
        logger.debug("************************************************************")

        logger.debug("Evaluating accuracy...")
        accuracy_evaluator = self.get_accuracy_evaluator()
        try:
            self.accuracy_result: AccuracyResult = accuracy_evaluator.run(answer_to_evaluate, stream=False)  # type: ignore
        except Exception as e:
            logger.error(f"Failed to evaluate accuracy: {e}")
            return None

        if self.accuracy_result is not None:
            self.result = EvalResult(
                accuracy_score=self.accuracy_result.score,
                accuracy_reason=self.accuracy_result.reason,
            )

        # -*- Save result to file if save_result_to_file is set
        if self.save_result_to_file is not None and self.result is not None:
            try:
                fn = Path(self.save_result_to_file.format(name=self.name, eval_id=self.eval_id))
                fn.parent.mkdir(parents=True, exist_ok=True)
                fn.write_text(self.result.model_dump_json(indent=4))
            except Exception as e:
                logger.warning(f"Failed to save result to file: {e}")

        logger.debug(f"*********** Evaluation End: {self.eval_id} ***********")
        return self.result

    def print_result(self, answer: Optional[Union[str, Callable]] = None) -> Optional[EvalResult]:
        from phi.cli.console import console
        from rich.table import Table
        from rich.progress import Progress, SpinnerColumn, TextColumn
        from rich.box import ROUNDED

        response_timer = Timer()
        response_timer.start()
        with Progress(SpinnerColumn(spinner_name="dots"), TextColumn("{task.description}"), transient=True) as progress:
            progress.add_task("Working...")
            result: Optional[EvalResult] = self.run(answer=answer)

        response_timer.stop()
        if result is None:
            return None

        console.print("\n", style="")  # This adds one blank line
        table = Table(
            box=ROUNDED,
            border_style="blue",
            show_header=False,
            title="[ Evaluation Result ]",
            title_style="bold sky_blue1",
            title_justify="center",
        )
        table.add_row("Question", self.question)
        table.add_row("Answer", self.answer)
        table.add_row("Ideal Answer", self.ideal_answer)
        table.add_row("Accuracy Score", f"{str(result.accuracy_score)}/10")
        table.add_row("Accuracy Reason", result.accuracy_reason)
        table.add_row("Time Taken", f"{response_timer.elapsed:.1f}s")
        console.print(table)

        return result
