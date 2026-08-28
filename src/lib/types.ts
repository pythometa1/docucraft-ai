export type ProjectStatus = "Completed" | "In Progress" | "Pending" | "Failed";

export type FunctionKey =
  | "Human Resources"
  | "Clinical"
  | "Quality-CMC"
  | "Quality"
  | "Safety"
  | "Medical Affairs"
  | "Marketing"
  | "Legal"
  | "Regulatory Affairs";

export interface TemplateFile {
  id: string;
  name: string;
  size: string;
  uploadedAt: string;
  uploadedBy: string;
  // Whether the template has been read yet. A project cannot move past its
  // first stage without this: nothing downstream knows what data the letter
  // needs until the template has been compiled into a manifest.
  manifestId?: string;
  manifestStatus?: string;
  fieldCount: number;
  conditionCount: number;
}

export interface SourceFile {
  id: string;
  name: string;
  type: "csv" | "xlsx" | "json" | "pdf" | "docx" | "txt";
  size: string;
  rows?: number;
  uploadedAt: string;
  // The version everything downstream addresses: binding suggestions and
  // generation are per source *version*, not per file.
  currentVersionId?: string;
}

export interface GeneratedDoc {
  id: string;
  filename: string;
  // The download endpoint is /document-versions/{id}/download, so the version
  // is what a download needs -- `id` here is the GeneratedDocument, and passing
  // it produced a 404 on every document the new flow generates.
  currentVersionId?: string;
  status: string;
  generatedAt: string;
  size: string;
  generatedBy: string;
}

export interface Project {
  id: string;
  projectId: string; // display number e.g. 51255
  name: string;
  description?: string;
  documentType: string;
  function: FunctionKey;
  region: string;
  language: string;
  createdAt: string;
  modifiedAt: string;
  status: ProjectStatus;
  templates: TemplateFile[];
  sources: SourceFile[];
  generated: GeneratedDoc[];
  generationMethod?: "ai" | "chat" | "manual" | "hybrid";
}
